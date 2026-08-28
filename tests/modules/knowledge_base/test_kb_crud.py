"""Knowledge base CRUD over HTTP."""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.modules.knowledge_base.helpers import create_kb, headers, upload_ok


async def test_create_starts_empty_on_the_direct_tier(client: AsyncClient) -> None:
    auth = await headers(client)

    knowledge_base = await create_kb(client, auth, description="Paint ranges and prices.")

    assert knowledge_base["retrievalTier"] == "direct"
    assert knowledge_base["sourceCount"] == 0
    assert knowledge_base["agentCount"] == 0
    assert knowledge_base["description"] == "Paint ranges and prices."


async def test_the_keyword_tier_can_be_chosen_at_creation(client: AsyncClient) -> None:
    auth = await headers(client)

    knowledge_base = await create_kb(client, auth, retrievalTier="keyword")

    assert knowledge_base["retrievalTier"] == "keyword"


async def test_the_vector_tier_is_not_selectable_in_v1(client: AsyncClient) -> None:
    """Tier 3 is v2 (spec §5.2.2). Accepting the value would let a tenant pick a tier that does
    nothing."""
    auth = await headers(client)

    response = await client.post(
        "/knowledge-bases", json={"name": "Vectors", "retrievalTier": "vector"}, headers=auth
    )

    assert response.status_code == 422


async def test_duplicate_names_are_rejected_within_a_tenant(client: AsyncClient) -> None:
    auth = await headers(client)
    await create_kb(client, auth, name="Policies")

    response = await client.post("/knowledge-bases", json={"name": "policies"}, headers=auth)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "KB_NAME_TAKEN"


async def test_two_tenants_may_use_the_same_name(client: AsyncClient) -> None:
    first = await headers(client)
    second = await headers(client)
    await create_kb(client, first, name="Policies")

    response = await client.post("/knowledge-bases", json={"name": "Policies"}, headers=second)

    assert response.status_code == 201


async def test_the_detail_response_counts_sources(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    await upload_ok(client, auth, knowledge_base["id"], "a.txt", b"Matt white is $45.")
    await upload_ok(client, auth, knowledge_base["id"], "b.txt", b"Gloss white is $52.")

    body = (await client.get(f"/knowledge-bases/{knowledge_base['id']}", headers=auth)).json()

    assert body["value"]["sourceCount"] == 2


async def test_update_changes_only_what_was_sent(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth, description="Original.")

    response = await client.patch(
        f"/knowledge-bases/{knowledge_base['id']}",
        json={"retrievalTier": "keyword"},
        headers=auth,
    )

    assert response.status_code == 200
    assert response.json()["value"]["retrievalTier"] == "keyword"
    assert response.json()["value"]["description"] == "Original."


async def test_renaming_onto_another_name_is_rejected(client: AsyncClient) -> None:
    auth = await headers(client)
    await create_kb(client, auth, name="Policies")
    other = await create_kb(client, auth, name="Prices")

    response = await client.patch(
        f"/knowledge-bases/{other['id']}", json={"name": "Policies"}, headers=auth
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "KB_NAME_TAKEN"


async def test_a_knowledge_base_can_keep_its_own_name(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth, name="Policies")

    response = await client.patch(
        f"/knowledge-bases/{knowledge_base['id']}",
        json={"name": "Policies", "description": "Updated."},
        headers=auth,
    )

    assert response.status_code == 200
    assert response.json()["value"]["description"] == "Updated."


async def test_delete_removes_the_knowledge_base_and_its_sources(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    source = await upload_ok(client, auth, knowledge_base["id"], "a.txt", b"Matt white is $45.")

    assert (
        await client.delete(f"/knowledge-bases/{knowledge_base['id']}", headers=auth)
    ).status_code == 200

    assert (
        await client.get(f"/knowledge-bases/{knowledge_base['id']}", headers=auth)
    ).status_code == 404
    usage = (await client.get("/knowledge-bases/usage", headers=auth)).json()["value"]
    assert usage["usedBytes"] == 0, f"source {source['id']} should no longer count"


async def test_an_unknown_knowledge_base_is_reported_as_missing(client: AsyncClient) -> None:
    auth = await headers(client)

    response = await client.get(f"/knowledge-bases/{uuid.uuid4()}", headers=auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "KB_NOT_FOUND"


async def test_the_list_is_newest_first_and_paginated(client: AsyncClient) -> None:
    auth = await headers(client)
    for index in range(3):
        await create_kb(client, auth, name=f"KB {index}")

    body = (await client.get("/knowledge-bases?pageSize=2", headers=auth)).json()

    assert body["meta"]["totalItems"] == 3
    assert body["meta"]["totalPages"] == 2
    assert len(body["value"]) == 2


async def test_usage_reports_stored_bytes_against_the_limits(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    await upload_ok(client, auth, knowledge_base["id"], "a.txt", b"x" * 500)

    usage = (await client.get("/knowledge-bases/usage", headers=auth)).json()["value"]

    assert usage["usedBytes"] == 500
    assert usage["limitBytes"] > usage["maxSourceBytes"]
