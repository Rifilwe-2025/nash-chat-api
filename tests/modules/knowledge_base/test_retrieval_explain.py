"""The retrieval explain endpoint over HTTP (spec §5.2).

This is the surface a developer uses to answer "why did the agent say that?" before there is an
agent to ask, so the tests care as much about the *reasoning* it reports as about the passages.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from httpx import AsyncClient

from tests.modules.knowledge_base.helpers import create_agent, create_kb, headers

RETURNS = (
    "Paint may be returned within 30 days with a receipt. Tinted paint is final sale and cannot "
    "be returned."
)
DELIVERY = "Delivery is next working day within Harare for orders placed before 2pm."


async def add_manual(
    client: AsyncClient, auth: dict[str, str], kb_id: str, title: str, body: str
) -> None:
    response = await client.post(
        f"/knowledge-bases/{kb_id}/sources/manual",
        json={"title": title, "body": body},
        headers=auth,
    )
    assert response.status_code == 201, response.text


async def explain(
    client: AsyncClient, auth: dict[str, str], kb_id: str, query: str, **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        f"/knowledge-bases/{kb_id}/retrieval/explain",
        json={"query": query, **extra},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    value: dict[str, Any] = response.json()["value"]
    return value


async def stocked(client: AsyncClient, auth: dict[str, str], **kwargs: Any) -> dict[str, Any]:
    knowledge_base = await create_kb(client, auth, **kwargs)
    await add_manual(client, auth, knowledge_base["id"], "Returns", RETURNS)
    await add_manual(client, auth, knowledge_base["id"], "Delivery", DELIVERY)
    return knowledge_base


async def test_a_small_knowledge_base_explains_as_direct_injection(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100_000)
    auth = await headers(client)
    knowledge_base = await stocked(client, auth)

    body = await explain(client, auth, knowledge_base["id"], "Can I return tinted paint?")

    assert body["tier"] == "direct"
    assert body["tierForced"] is False
    assert body["hasContext"] is True
    assert "fit within" in body["tierReason"]
    assert len(body["passages"]) == 2
    assert body["retrievedCharacters"] > 0


async def test_a_large_knowledge_base_explains_as_keyword_search(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100)
    auth = await headers(client)
    knowledge_base = await stocked(client, auth)

    body = await explain(client, auth, knowledge_base["id"], "tinted paint")

    assert body["tier"] == "keyword"
    assert "exceed" in body["tierReason"]
    assert body["consideredCharacters"] > body["budgetCharacters"]
    assert body["passages"][0]["score"] > 0


async def test_the_explanation_names_the_source_of_every_passage(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100_000)
    auth = await headers(client)
    knowledge_base = await stocked(client, auth)

    body = await explain(client, auth, knowledge_base["id"], "returns")

    citation = body["passages"][0]["citation"]
    assert citation["sourceName"] in {"Returns", "Delivery"}
    assert citation["kbId"] == knowledge_base["id"]
    assert citation["sourceType"] == "manual"
    assert "url" not in citation, "exclude_none drops the URL for a non-URL source"


async def test_an_off_topic_query_explains_why_nothing_came_back(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100)
    auth = await headers(client)
    knowledge_base = await stocked(client, auth)

    body = await explain(client, auth, knowledge_base["id"], "elephant migration patterns")

    assert body["hasContext"] is False
    assert body["noContextReason"] == "no_match"
    assert body["passages"] == []


async def test_an_empty_knowledge_base_explains_that_it_is_empty(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth, name="Nothing yet")

    body = await explain(client, auth, knowledge_base["id"], "anything")

    assert body["hasContext"] is False
    assert body["noContextReason"] == "empty_knowledge_base"


async def test_a_pinned_tier_is_reported_as_forced(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100_000)
    auth = await headers(client)
    knowledge_base = await stocked(client, auth, retrievalTier="keyword")

    body = await explain(client, auth, knowledge_base["id"], "tinted paint")

    assert body["tier"] == "keyword"
    assert body["tierForced"] is True
    assert "pinned" in body["tierReason"]


async def test_the_target_model_changes_the_budget(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    """Two agents on different models can get different tiers from the same knowledge base."""
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=10_000_000, KB_CONTEXT_BUDGET_FRACTION=0.25)
    auth = await headers(client)
    knowledge_base = await stocked(client, auth)

    narrow = await explain(client, auth, knowledge_base["id"], "returns", model="gpt-4o")
    wide = await explain(client, auth, knowledge_base["id"], "returns", model="gemini-2.0-flash")

    assert wide["budgetCharacters"] > narrow["budgetCharacters"]


async def test_explaining_for_an_agent_covers_all_its_knowledge_bases(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(KB_DIRECT_INJECTION_MAX_CHARS=100_000)
    auth = await headers(client)
    agent = await create_agent(client, auth, "Sales Assistant")
    policies = await create_kb(client, auth, name="Policies")
    await add_manual(client, auth, policies["id"], "Returns", RETURNS)
    logistics = await create_kb(client, auth, name="Logistics")
    await add_manual(client, auth, logistics["id"], "Delivery", DELIVERY)
    for knowledge_base in (policies, logistics):
        await client.put(
            f"/knowledge-bases/{knowledge_base['id']}/agents/{agent['id']}", headers=auth
        )

    response = await client.post(
        f"/knowledge-bases/retrieval/explain?agentId={agent['id']}",
        json={"query": "returns and delivery"},
        headers=auth,
    )

    assert response.status_code == 200, response.text
    body = response.json()["value"]
    assert {passage["citation"]["kbId"] for passage in body["passages"]} == {
        policies["id"],
        logistics["id"],
    }


async def test_explaining_for_another_tenants_agent_is_reported_as_missing(
    client: AsyncClient,
) -> None:
    first = await headers(client)
    second = await headers(client)
    foreign_agent = await create_agent(client, first, "Their Agent")

    response = await client.post(
        f"/knowledge-bases/retrieval/explain?agentId={foreign_agent['id']}",
        json={"query": "margins"},
        headers=second,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"


async def test_explaining_against_another_tenants_knowledge_base_is_reported_as_missing(
    client: AsyncClient,
) -> None:
    first = await headers(client)
    second = await headers(client)
    theirs = await create_kb(client, first, name="Confidential")
    await add_manual(client, first, theirs["id"], "Margins", "Our margin is 42 percent.")

    response = await client.post(
        f"/knowledge-bases/{theirs['id']}/retrieval/explain",
        json={"query": "margin"},
        headers=second,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "KB_NOT_FOUND"
    assert "42 percent" not in response.text


async def test_an_empty_query_is_rejected(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    response = await client.post(
        f"/knowledge-bases/{knowledge_base['id']}/retrieval/explain",
        json={"query": ""},
        headers=auth,
    )

    assert response.status_code == 422


async def test_the_explain_endpoints_require_authentication(client: AsyncClient) -> None:
    unknown = uuid.uuid4()

    assert (
        await client.post(f"/knowledge-bases/{unknown}/retrieval/explain", json={"query": "x"})
    ).status_code == 401
    assert (
        await client.post(
            f"/knowledge-bases/retrieval/explain?agentId={unknown}", json={"query": "x"}
        )
    ).status_code == 401
