"""The failure report (spec §5.8).

The phase's bar is that "every failure class above is queryable through the API", so each of the
five is seeded in the table that actually owns it and then read back through the one endpoint. That
is also the assertion that matters architecturally: analytics does not keep its own copy of these
failures, it reads them where the module that understands them wrote them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.analytics.domain.models import EventCategory, PlatformEvent
from src.modules.channels.domain.models import (
    ChannelConfig,
    ChannelType,
    WebhookEndpoint,
    WebhookEvent,
)
from src.modules.channels.whatsapp.domain.models import (
    DeliveryStatus,
    MessageDirection,
    WhatsAppMessage,
)
from src.modules.knowledge_base.domain.models import (
    KbSource,
    KnowledgeBase,
    SourceStatus,
    SourceType,
)
from src.modules.tools.domain.models import AgentTool, ToolCallLog, ToolOutcome
from tests.modules.analytics.helpers import make_agent
from tests.modules.analytics.test_usage import owner


def classes(report: dict[str, Any]) -> dict[str, Any]:
    return {entry["kind"]: entry for entry in report["classes"]}


async def test_a_failed_ingestion_is_reported(client: AsyncClient, session: AsyncSession) -> None:
    auth, tenant_id = await owner(client)

    kb = KnowledgeBase(tenant_id=tenant_id, name="Policies")
    session.add(kb)
    await session.flush()
    session.add(
        KbSource(
            tenant_id=tenant_id,
            kb_id=kb.id,
            name="refund-policy.pdf",
            type=SourceType.FILE,
            status=SourceStatus.FAILED,
            error_detail="The model could not read this file.",
        )
    )
    await session.flush()

    report = (await client.get("/analytics/failures", headers=auth)).json()["value"]

    ingestion = classes(report)["ingestion"]
    assert ingestion["count"] == 1
    assert ingestion["recent"][0]["subject"] == "refund-policy.pdf"
    assert ingestion["recent"][0]["code"] == "INGESTION_FAILED"
    assert "could not read" in ingestion["recent"][0]["detail"]


async def test_a_failed_tool_call_is_reported(client: AsyncClient, session: AsyncSession) -> None:
    """``tool_call`` is not tenant-scoped, so this also proves the join runs through the tool."""
    auth, tenant_id = await owner(client)
    agent = await make_agent(session, tenant_id)

    tool = AgentTool(
        tenant_id=tenant_id,
        agent_id=agent.id,
        name="check_order_status",
        description="Look up an order.",
        endpoint_url="https://api.example.test/orders/{orderId}",
    )
    session.add(tool)
    await session.flush()
    session.add(
        ToolCallLog(
            tool_id=tool.id,
            outcome=ToolOutcome.TIMED_OUT,
            arguments_json={"orderId": "A-1"},
            duration_ms=8000,
            error_detail="The endpoint did not answer in time.",
        )
    )
    await session.flush()

    report = (await client.get("/analytics/failures", headers=auth)).json()["value"]

    tools = classes(report)["tool"]
    assert tools["count"] == 1
    assert tools["recent"][0]["code"] == "TIMED_OUT"
    assert tools["recent"][0]["subject"] == "check_order_status"
    assert tools["recent"][0]["agentId"] == str(agent.id)


async def test_an_undelivered_whatsapp_message_is_reported(
    client: AsyncClient, session: AsyncSession
) -> None:
    auth, tenant_id = await owner(client)
    agent = await make_agent(session, tenant_id)

    connection = ChannelConfig(
        tenant_id=tenant_id, agent_id=agent.id, channel_type=ChannelType.WHATSAPP
    )
    session.add(connection)
    await session.flush()
    session.add(
        WhatsAppMessage(
            tenant_id=tenant_id,
            connection_id=connection.id,
            agent_id=agent.id,
            direction=MessageDirection.INBOUND,
            wa_contact_id="263771234567",
            status=DeliveryStatus.FAILED,
            error_detail="Answering the message took too long and was stopped.",
        )
    )
    await session.flush()

    report = (await client.get("/analytics/failures", headers=auth)).json()["value"]

    channel = classes(report)["channel"]
    assert channel["count"] == 1
    assert channel["recent"][0]["code"] == "WHATSAPP_DELIVERY_FAILED"
    # The contact, not the message body: a failure report must not become a second transcript.
    assert channel["recent"][0]["subject"] == "263771234567"


async def test_a_failing_webhook_endpoint_is_reported(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Webhooks report current state rather than events, because that is what channels keeps."""
    auth, tenant_id = await owner(client)

    session.add(
        WebhookEndpoint(
            tenant_id=tenant_id,
            url="https://hooks.example.test/nash",
            secret="whsec_test",
            events=[WebhookEvent.CONVERSATION_ESCALATED.value],
            failure_count=4,
            last_error="HTTP 500",
            last_delivery_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    await session.flush()

    report = (await client.get("/analytics/failures", headers=auth)).json()["value"]

    webhook = classes(report)["webhook"]
    assert webhook["count"] == 1
    assert webhook["recent"][0]["code"] == "CONSECUTIVE_FAILURES_4"
    assert webhook["recent"][0]["detail"] == "HTTP 500"


async def test_a_provider_error_is_reported(client: AsyncClient, session: AsyncSession) -> None:
    """The one failure with no other durable home — see analytics/domain/models.py."""
    auth, tenant_id = await owner(client)
    agent = await make_agent(session, tenant_id)

    session.add(
        PlatformEvent(
            tenant_id=tenant_id,
            agent_id=agent.id,
            category=EventCategory.PROVIDER_ERROR,
            code="PROVIDER_UNAVAILABLE",
            detail="The agent's model could not be reached.",
            meta_json={"channel": "whatsapp", "subject": "263771234567"},
        )
    )
    await session.flush()

    report = (await client.get("/analytics/failures", headers=auth)).json()["value"]

    provider = classes(report)["provider"]
    assert provider["count"] == 1
    assert provider["recent"][0]["code"] == "PROVIDER_UNAVAILABLE"
    assert provider["recent"][0]["subject"] == "263771234567"


async def test_an_empty_tenant_reports_no_failures(client: AsyncClient) -> None:
    auth, _ = await owner(client)

    report = (await client.get("/analytics/failures", headers=auth)).json()["value"]

    assert report["total"] == 0
    assert {entry["kind"] for entry in report["classes"]} == {
        "ingestion",
        "provider",
        "webhook",
        "channel",
        "tool",
    }


async def test_the_recent_limit_bounds_each_class(
    client: AsyncClient, session: AsyncSession
) -> None:
    auth, tenant_id = await owner(client)
    kb = KnowledgeBase(tenant_id=tenant_id, name="Policies")
    session.add(kb)
    await session.flush()
    for index in range(4):
        session.add(
            KbSource(
                tenant_id=tenant_id,
                kb_id=kb.id,
                name=f"broken-{index}.pdf",
                type=SourceType.FILE,
                status=SourceStatus.FAILED,
            )
        )
    await session.flush()

    report = (
        await client.get("/analytics/failures", params={"recentLimit": 2}, headers=auth)
    ).json()["value"]

    ingestion = classes(report)["ingestion"]
    assert ingestion["count"] == 4
    assert len(ingestion["recent"]) == 2


async def test_failures_requires_a_token(client: AsyncClient) -> None:
    assert (await client.get("/analytics/failures")).status_code == 401


async def test_another_tenants_failures_are_invisible(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The isolation that matters most here: analytics reads five other modules' tables."""
    _, other_tenant = await owner(client)
    kb = KnowledgeBase(tenant_id=other_tenant, name="Theirs")
    session.add(kb)
    await session.flush()
    session.add(
        KbSource(
            tenant_id=other_tenant,
            kb_id=kb.id,
            name="their-secret.pdf",
            type=SourceType.FILE,
            status=SourceStatus.FAILED,
        )
    )
    session.add(
        PlatformEvent(
            tenant_id=other_tenant,
            category=EventCategory.PROVIDER_ERROR,
            code="PROVIDER_UNAVAILABLE",
        )
    )
    await session.flush()

    auth, _ = await owner(client)
    report = (await client.get("/analytics/failures", headers=auth)).json()["value"]

    assert report["total"] == 0
