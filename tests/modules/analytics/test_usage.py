"""The usage dashboard (spec §5.8).

The bar the phase sets is that "a dashboard-shaped payload for one agent returns counts and costs
that reconcile with the stored message rows". So these tests seed known rows and assert the exact
numbers — not that a field exists, but that the total is the sum of what was written.
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.conversations.domain.models import Channel
from tests.modules.analytics.helpers import Turn, days_ago, make_agent, seed_conversation
from tests.modules.auth.test_auth_flow import auth_header, signup


async def owner(client: AsyncClient) -> tuple[dict[str, str], uuid.UUID]:
    """Sign up over HTTP and keep both the token and the tenant it belongs to."""
    value = await signup(client)
    return auth_header(value["tokens"]), uuid.UUID(value["user"]["tenantId"])


async def test_totals_reconcile_with_the_stored_messages(
    client: AsyncClient, session: AsyncSession
) -> None:
    auth, tenant_id = await owner(client)
    agent = await make_agent(session, tenant_id)

    await seed_conversation(
        session,
        tenant_id,
        agent.id,
        [
            Turn(prompt_tokens=100, completion_tokens=20, cost_micro_usd=1_500),
            Turn(prompt_tokens=140, completion_tokens=30, cost_micro_usd=2_000),
        ],
    )
    await seed_conversation(
        session,
        tenant_id,
        agent.id,
        [Turn(prompt_tokens=60, completion_tokens=10, cost_micro_usd=800)],
    )

    response = await client.get("/analytics/usage", headers=auth)

    assert response.status_code == 200, response.text
    report: dict[str, Any] = response.json()["value"]
    assert report["conversations"]["started"] == 2
    assert report["messages"]["total"] == 6
    assert report["messages"]["user"] == 3
    assert report["messages"]["assistant"] == 3
    assert report["messages"]["promptTokens"] == 300
    assert report["messages"]["completionTokens"] == 60
    assert report["messages"]["totalTokens"] == 360
    assert report["messages"]["costMicroUsd"] == 4_300
    assert report["messages"]["pricedMessages"] == 3


async def test_a_model_with_no_price_records_tokens_but_no_cost(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The platform never guesses at a price it was not given (``shared/llm/pricing.py``).

    ``pricedMessages`` below ``assistant`` is how a reader tells "this was free" from "we do not
    know what this cost" — without it the cost figure would read as a total when it is a floor.
    """
    auth, tenant_id = await owner(client)
    agent = await make_agent(session, tenant_id)

    await seed_conversation(session, tenant_id, agent.id, [Turn(cost_micro_usd=None)])

    report = (await client.get("/analytics/usage", headers=auth)).json()["value"]

    assert report["messages"]["assistant"] == 1
    assert report["messages"]["totalTokens"] == 120
    assert report["messages"]["costMicroUsd"] == 0
    assert report["messages"]["pricedMessages"] == 0


async def test_preview_traffic_is_excluded_unless_asked_for(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A tenant testing their own agent is not a customer using it."""
    auth, tenant_id = await owner(client)
    agent = await make_agent(session, tenant_id)

    await seed_conversation(session, tenant_id, agent.id, [Turn()], channel=Channel.WEB)
    await seed_conversation(session, tenant_id, agent.id, [Turn()], channel=Channel.PREVIEW)

    without = (await client.get("/analytics/usage", headers=auth)).json()["value"]
    with_preview = (await client.get("/analytics/usage?includePreview=true", headers=auth)).json()[
        "value"
    ]

    assert without["conversations"]["started"] == 1
    assert without["includesPreview"] is False
    assert with_preview["conversations"]["started"] == 2
    assert with_preview["includesPreview"] is True


async def test_the_window_bounds_what_is_counted(
    client: AsyncClient, session: AsyncSession
) -> None:
    auth, tenant_id = await owner(client)
    agent = await make_agent(session, tenant_id)

    await seed_conversation(session, tenant_id, agent.id, [Turn()], at=days_ago(1))
    await seed_conversation(session, tenant_id, agent.id, [Turn()], at=days_ago(90))

    default_window = (await client.get("/analytics/usage", headers=auth)).json()["value"]
    # Through `params` rather than an f-string: an ISO timestamp ends in "+00:00", and a raw "+"
    # in a query string is a space.
    wide = (
        await client.get(
            "/analytics/usage", params={"from": days_ago(120).isoformat()}, headers=auth
        )
    ).json()["value"]

    assert default_window["conversations"]["started"] == 1
    assert wide["conversations"]["started"] == 2


async def test_a_window_longer_than_the_maximum_is_refused(client: AsyncClient) -> None:
    """A refusal rather than a silent truncation — a chart labelled with a span it did not cover
    is worse than an error."""
    auth, _ = await owner(client)

    response = await client.get(
        "/analytics/usage", params={"from": days_ago(400).isoformat()}, headers=auth
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ANALYTICS_WINDOW_TOO_LONG"


async def test_an_inverted_window_is_refused(client: AsyncClient) -> None:
    auth, _ = await owner(client)

    response = await client.get(
        "/analytics/usage",
        params={"from": days_ago(1).isoformat(), "to": days_ago(5).isoformat()},
        headers=auth,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ANALYTICS_WINDOW_INVALID"


async def test_quality_signals_count_the_markers_the_engine_wrote(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The "I don't know" rate is measured from ``hasContext``, not by reading the answer's text."""
    auth, tenant_id = await owner(client)
    agent = await make_agent(session, tenant_id)

    await seed_conversation(
        session,
        tenant_id,
        agent.id,
        [Turn(has_context=True), Turn(has_context=False), Turn(guardrail="declined")],
    )
    await seed_conversation(session, tenant_id, agent.id, [Turn()], escalated=True)

    quality = (await client.get("/analytics/usage", headers=auth)).json()["value"]["quality"]

    assert quality["answered"] == 4
    assert quality["withoutContext"] == 1
    assert quality["declined"] == 1
    assert quality["fallbackRate"] == 0.25
    assert quality["conversations"] == 2
    assert quality["escalated"] == 1
    assert quality["escalationRate"] == 0.5


async def test_the_daily_series_buckets_by_day(client: AsyncClient, session: AsyncSession) -> None:
    auth, tenant_id = await owner(client)
    agent = await make_agent(session, tenant_id)

    await seed_conversation(session, tenant_id, agent.id, [Turn()], at=days_ago(2))
    await seed_conversation(session, tenant_id, agent.id, [Turn()], at=days_ago(2))
    await seed_conversation(session, tenant_id, agent.id, [Turn()], at=days_ago(1))

    daily = (await client.get("/analytics/usage", headers=auth)).json()["value"]["daily"]

    assert [point["messages"] for point in daily] == [4, 2]
    assert [point["conversations"] for point in daily] == [2, 1]
    assert daily[0]["day"] < daily[1]["day"]


async def test_spend_is_split_by_provider_and_model(
    client: AsyncClient, session: AsyncSession
) -> None:
    auth, tenant_id = await owner(client)
    agent = await make_agent(session, tenant_id)

    await seed_conversation(session, tenant_id, agent.id, [Turn(cost_micro_usd=2_000)])

    models = (await client.get("/analytics/usage", headers=auth)).json()["value"]["models"]

    assert len(models) == 1
    assert models[0]["provider"] == "gemini"
    assert models[0]["model"] == "gemini-2.0-flash"
    assert models[0]["costMicroUsd"] == 2_000


async def test_the_per_agent_report_covers_only_that_agent(
    client: AsyncClient, session: AsyncSession
) -> None:
    auth, tenant_id = await owner(client)
    support = await make_agent(session, tenant_id, name="Support")
    sales = await make_agent(session, tenant_id, name="Sales")

    await seed_conversation(session, tenant_id, support.id, [Turn(), Turn()])
    await seed_conversation(session, tenant_id, sales.id, [Turn()])

    report = (await client.get(f"/agents/{support.id}/analytics", headers=auth)).json()["value"]

    assert report["agentId"] == str(support.id)
    assert report["messages"]["assistant"] == 2
    assert report["channels"][0]["channel"] == "web"


async def test_an_unknown_agent_is_a_404(client: AsyncClient) -> None:
    auth, _ = await owner(client)

    response = await client.get(f"/agents/{uuid.uuid4()}/analytics", headers=auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"


async def test_analytics_requires_a_token(client: AsyncClient) -> None:
    assert (await client.get("/analytics/usage")).status_code == 401
