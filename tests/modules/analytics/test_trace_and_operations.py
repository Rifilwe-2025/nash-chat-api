"""The conversation trace and the operator metrics endpoint (spec §5.8).

Two surfaces that look unrelated and are here together because they answer the same question from
opposite ends: *why did this go wrong?* The trace answers it for one conversation, from durable
rows a tenant owns. The operations endpoint answers it for the deployment, from in-memory counters
an operator owns — which is exactly why the two are authenticated differently.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.observability import HTTP_REQUESTS, metrics
from tests.modules.analytics.helpers import Turn, make_agent, seed_conversation
from tests.modules.analytics.test_usage import owner

OPERATOR_TOKEN = "operator-secret-for-tests"


# -- the citation trace --------------------------------------------------------------


async def test_the_trace_shows_what_grounded_each_answer(
    client: AsyncClient, session: AsyncSession
) -> None:
    auth, tenant_id = await owner(client)
    agent = await make_agent(session, tenant_id)
    conversation = await seed_conversation(
        session,
        tenant_id,
        agent.id,
        [Turn(citations=[{"sourceId": "s-1", "name": "refund-policy.pdf"}])],
    )

    response = await client.get(f"/analytics/conversations/{conversation.id}/trace", headers=auth)

    assert response.status_code == 200, response.text
    entries = response.json()["value"]
    assert [entry["role"] for entry in entries] == ["user", "assistant"]

    reply = entries[1]
    assert reply["citations"] == [{"sourceId": "s-1", "name": "refund-policy.pdf"}]
    assert reply["tier"] == "keyword"
    assert reply["hasContext"] is True
    assert reply["promptTokens"] == 100
    assert reply["costMicroUsd"] == 1_500


async def test_the_trace_marks_a_fallback_and_a_guardrail(
    client: AsyncClient, session: AsyncSession
) -> None:
    """``hasContext: false`` is the "I don't know" case; ``guardrail`` means a rule answered."""
    auth, tenant_id = await owner(client)
    agent = await make_agent(session, tenant_id)
    conversation = await seed_conversation(
        session, tenant_id, agent.id, [Turn(has_context=False), Turn(guardrail="declined")]
    )

    entries = (
        await client.get(f"/analytics/conversations/{conversation.id}/trace", headers=auth)
    ).json()["value"]

    assert entries[1]["hasContext"] is False
    assert entries[3]["guardrail"] == "declined"


async def test_another_tenants_conversation_is_a_404(
    client: AsyncClient, session: AsyncSession
) -> None:
    _, other_tenant = await owner(client)
    agent = await make_agent(session, other_tenant)
    theirs = await seed_conversation(session, other_tenant, agent.id, [Turn()])

    auth, _ = await owner(client)
    response = await client.get(f"/analytics/conversations/{theirs.id}/trace", headers=auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


async def test_an_unknown_conversation_is_a_404(client: AsyncClient) -> None:
    auth, _ = await owner(client)

    response = await client.get(f"/analytics/conversations/{uuid.uuid4()}/trace", headers=auth)

    assert response.status_code == 404


# -- operator metrics ----------------------------------------------------------------


@pytest.fixture
def operator(config_override: Callable[..., None]) -> dict[str, str]:
    config_override(OBSERVABILITY_OPERATOR_TOKEN=OPERATOR_TOKEN)
    return {"X-Operator-Token": OPERATOR_TOKEN}


async def test_operations_are_closed_when_no_token_is_configured(client: AsyncClient) -> None:
    """A deployment that has not thought about this must not be publishing its counters."""
    response = await client.get("/analytics/operations")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "METRICS_DISABLED"


async def test_operations_reject_a_wrong_token(
    client: AsyncClient, operator: dict[str, str]
) -> None:
    response = await client.get(
        "/analytics/operations", headers={"X-Operator-Token": "not-the-secret"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "OPERATOR_TOKEN_INVALID"


async def test_a_tenant_token_does_not_open_operations(
    client: AsyncClient, operator: dict[str, str]
) -> None:
    """These numbers span every tenant, so a tenant credential is the wrong one for them."""
    auth, _ = await owner(client)

    assert (await client.get("/analytics/operations", headers=auth)).status_code == 403


async def test_operations_report_request_counters(
    client: AsyncClient, operator: dict[str, str]
) -> None:
    metrics.reset()
    await client.get("/health")

    response = await client.get("/analytics/operations", headers=operator)

    assert response.status_code == 200, response.text
    snapshot = response.json()["value"]
    assert snapshot["seriesDropped"] == 0

    health = [
        counter
        for counter in snapshot["counters"]
        if counter["name"] == HTTP_REQUESTS and counter["labels"]["route"] == "/health"
    ]
    assert health and health[0]["value"] >= 1
