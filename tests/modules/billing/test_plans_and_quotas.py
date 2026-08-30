"""Plan limits and usage metering (spec §5.9, Phase 14).

Three things are worth pinning down, and each has bitten a billing system somewhere:

* a quota **refuses before the work**, not after — a limit checked once the provider has been paid
  is an accounting note;
* the refusal **says which limit and by how much**, because a bare "no" is a support ticket;
* metering rides the turn's own transaction, so a turn that fails does not bill for itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.billing.domain.models import UsageMetric
from src.modules.billing.domain.services import BillingService, current_period
from src.modules.billing.internal import plans
from tests.modules.analytics.helpers import Turn, make_agent, seed_conversation
from tests.modules.auth.test_auth_flow import auth_header, signup


async def owner(client: AsyncClient) -> tuple[dict[str, str], uuid.UUID]:
    value = await signup(client)
    return auth_header(value["tokens"]), uuid.UUID(value["user"]["tenantId"])


async def create_agent(client: AsyncClient, auth: dict[str, str], name: str) -> Any:
    return await client.post("/agents", json={"name": name, "persona": "Helpful."}, headers=auth)


# -- plan definitions ----------------------------------------------------------------


def test_the_built_in_plans_are_ordered_by_generosity() -> None:
    table = plans.plans()

    assert table["free"].agents < table["starter"].agents < table["pro"].agents
    assert table["free"].messages_per_month < table["pro"].messages_per_month


def test_configuration_overrides_only_the_limits_it_names(
    config_override: Callable[..., None],
) -> None:
    """Raising one ceiling should not mean restating the other two."""
    config_override(BILLING_PLANS="free=messages:9999")

    free = plans.for_tenant("free")
    assert free.messages_per_month == 9999
    assert free.agents == plans.DEFAULTS["free"].agents
    assert free.storage_bytes == plans.DEFAULTS["free"].storage_bytes


def test_a_malformed_entry_is_ignored_rather_than_fatal(
    config_override: Callable[..., None],
) -> None:
    """A typo in an environment variable must not lock every tenant out of their account."""
    config_override(BILLING_PLANS="this is not a plan;free=agents:3")

    assert plans.for_tenant("free").agents == 3


def test_an_unknown_plan_falls_back_to_free_not_to_unlimited() -> None:
    """An unknown plan is a data problem, and the safe reading does not hand out a free account."""
    assert plans.for_tenant("enterprise-platinum").agents == plans.DEFAULTS["free"].agents


def test_minus_one_means_unlimited(config_override: Callable[..., None]) -> None:
    config_override(BILLING_PLANS="free=agents:-1,messages:-1,storage:-1")

    plan = plans.for_tenant("free")
    assert plan.allows(plan.agents, current=10_000)


# -- enforcement ---------------------------------------------------------------------


@pytest.fixture
def one_agent_plan(config_override: Callable[..., None]) -> None:
    config_override(BILLING_PLANS="free=agents:1", BILLING_ENFORCE="true")


async def test_the_agent_ceiling_refuses_the_next_one(
    client: AsyncClient, one_agent_plan: None
) -> None:
    auth, _ = await owner(client)

    first = await create_agent(client, auth, "Support")
    second = await create_agent(client, auth, "Sales")

    assert first.status_code == 201
    assert second.status_code == 402
    assert second.json()["error"]["code"] == "PLAN_LIMIT_EXCEEDED"


async def test_the_refusal_names_the_limit_and_the_usage(
    client: AsyncClient, one_agent_plan: None
) -> None:
    """A 402 that says only "limit reached" is a support ticket."""
    auth, _ = await owner(client)
    await create_agent(client, auth, "Support")

    refusal = (await create_agent(client, auth, "Sales")).json()["error"]

    assert "free plan allows 1 agents" in refusal["detail"]
    assert "1 in use" in refusal["detail"]


async def test_deleting_an_agent_frees_its_slot(client: AsyncClient, one_agent_plan: None) -> None:
    """The agent limit is current state, not an accumulated counter."""
    auth, _ = await owner(client)
    created = await create_agent(client, auth, "Support")
    agent_id = created.json()["value"]["id"]

    assert (await client.delete(f"/agents/{agent_id}", headers=auth)).status_code == 200
    assert (await create_agent(client, auth, "Sales")).status_code == 201


async def test_limits_can_be_reported_without_being_enforced(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    """What a deployment runs while it is still settling on pricing (§9)."""
    config_override(BILLING_PLANS="free=agents:1", BILLING_ENFORCE="false")
    auth, _ = await owner(client)

    await create_agent(client, auth, "Support")
    second = await create_agent(client, auth, "Sales")

    assert second.status_code == 201

    plan = (await client.get("/billing/plan", headers=auth)).json()["value"]
    assert plan["enforced"] is False
    assert plan["agents"]["used"] == 2
    assert plan["agents"]["exceeded"] is True


async def test_the_storage_ceiling_refuses_an_upload_that_would_cross_it(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(BILLING_PLANS="free=storage:50", BILLING_ENFORCE="true")
    auth, _ = await owner(client)

    created = await client.post("/knowledge-bases", json={"name": "Policies"}, headers=auth)
    kb_id = created.json()["value"]["id"]

    response = await client.post(
        f"/knowledge-bases/{kb_id}/sources/manual",
        json={"title": "Policy", "body": "x" * 200},
        headers=auth,
    )

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "PLAN_LIMIT_EXCEEDED"


# -- metering ------------------------------------------------------------------------


async def test_metering_accumulates_into_the_period(
    session: AsyncSession, client: AsyncClient
) -> None:
    _, tenant_id = await owner(client)
    billing = BillingService(session, tenant_id)

    await billing.meter(messages=1, prompt_tokens=100, completion_tokens=20, cost_micro_usd=1_500)
    await billing.meter(messages=1, prompt_tokens=140, completion_tokens=30, cost_micro_usd=2_000)

    counters = await billing.counters.for_period(current_period())
    assert counters[UsageMetric.MESSAGES] == 2
    assert counters[UsageMetric.PROMPT_TOKENS] == 240
    assert counters[UsageMetric.COST_MICRO_USD] == 3_500


async def test_metering_zero_writes_nothing(session: AsyncSession, client: AsyncClient) -> None:
    """A guardrail turn costs no tokens, and a row of zeroes is noise in an invoice."""
    _, tenant_id = await owner(client)
    billing = BillingService(session, tenant_id)

    await billing.meter(messages=1)

    counters = await billing.counters.for_period(current_period())
    assert set(counters) == {UsageMetric.MESSAGES}


async def test_one_tenants_usage_is_invisible_to_another(
    session: AsyncSession, client: AsyncClient
) -> None:
    _, first = await owner(client)
    _, second = await owner(client)
    await BillingService(session, first).meter(messages=5)

    counters = await BillingService(session, second).counters.for_period(current_period())

    assert counters == {}


async def test_a_message_quota_refuses_before_the_provider_is_called(
    session: AsyncSession, client: AsyncClient, config_override: Callable[..., None]
) -> None:
    """The point of a message quota is to bound spend, so it has to come first."""
    config_override(BILLING_PLANS="free=messages:2", BILLING_ENFORCE="true")
    _, tenant_id = await owner(client)
    billing = BillingService(session, tenant_id)

    await billing.meter(messages=2)

    with pytest.raises(Exception) as raised:
        await billing.check_message_quota()

    assert "monthly messages" in str(raised.value)
    assert "resets" in str(raised.value)


# -- the endpoints -------------------------------------------------------------------


async def test_the_plan_endpoint_reports_limits_and_usage(
    client: AsyncClient, session: AsyncSession
) -> None:
    auth, tenant_id = await owner(client)
    agent = await make_agent(session, tenant_id)
    await seed_conversation(session, tenant_id, agent.id, [Turn()])
    await BillingService(session, tenant_id).meter(messages=3, prompt_tokens=300)

    plan = (await client.get("/billing/plan", headers=auth)).json()["value"]

    assert plan["plan"] == "free"
    assert plan["period"] == current_period()
    assert plan["messages"]["used"] == 3
    assert plan["messages"]["remaining"] == plan["messages"]["limit"] - 3
    assert plan["promptTokens"] == 300
    assert plan["agents"]["used"] == 1


async def test_an_unlimited_ceiling_reports_no_remaining(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    """`remaining` absent rather than an enormous number, so a progress bar cannot draw one."""
    config_override(BILLING_PLANS="free=messages:-1")
    auth, _ = await owner(client)

    plan = (await client.get("/billing/plan", headers=auth)).json()["value"]

    assert plan["messages"]["limit"] == -1
    assert "remaining" not in plan["messages"]


async def test_the_usage_endpoint_returns_periods_newest_first(
    client: AsyncClient, session: AsyncSession
) -> None:
    auth, tenant_id = await owner(client)
    billing = BillingService(session, tenant_id)
    await billing.counters.increment("2026-07", UsageMetric.MESSAGES, 40)
    await billing.counters.increment("2026-08", UsageMetric.MESSAGES, 12)

    history = (await client.get("/billing/usage", headers=auth)).json()["value"]["periods"]

    assert [entry["period"] for entry in history] == ["2026-08", "2026-07"]
    assert history[0]["messages"] == 12


async def test_billing_requires_a_token(client: AsyncClient) -> None:
    assert (await client.get("/billing/plan")).status_code == 401
    assert (await client.get("/billing/usage")).status_code == 401
