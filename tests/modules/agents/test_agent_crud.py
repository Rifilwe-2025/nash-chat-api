"""Agent CRUD, lifecycle, and versioning over HTTP."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.modules.auth.test_auth_flow import auth_header, signup

PUBLISHABLE: dict[str, Any] = {
    "name": "Sales Assistant",
    "persona": "You are the sales assistant for Nash Paints.",
    "modelProvider": "gemini",
    "modelSettings": {"model": "gemini-2.0-flash", "temperature": 0.5, "maxTokens": 800},
}


async def headers(client: AsyncClient) -> dict[str, str]:
    return auth_header((await signup(client))["tokens"])


async def create(client: AsyncClient, auth: dict[str, str], **overrides: Any) -> dict[str, Any]:
    payload = {"name": "Support Bot", **overrides}
    response = await client.post("/agents", json=payload, headers=auth)
    assert response.status_code == 201, response.text
    value: dict[str, Any] = response.json()["value"]
    return value


async def test_create_starts_as_a_draft_at_version_one(client: AsyncClient) -> None:
    auth = await headers(client)

    agent = await create(client, auth, persona="Helpful.")

    assert agent["status"] == "draft"
    assert agent["version"] == 1
    assert agent["persona"] == "Helpful."
    assert "modelProvider" not in agent, "exclude_none drops unset optionals"


async def test_create_accepts_full_configuration(client: AsyncClient) -> None:
    auth = await headers(client)

    agent = await create(
        client,
        auth,
        **PUBLISHABLE,
        engagementRules={
            "tone": "Warm",
            "dos": ["Offer colour matching"],
            "donts": ["Promise delivery dates"],
            "escalationTriggers": ["Refund request"],
        },
        guardrails={"restrictedTopics": ["Legal advice"], "fallbackResponse": "Let me check."},
    )

    assert agent["engagementRules"]["tone"] == "Warm"
    assert agent["engagementRules"]["dos"] == ["Offer colour matching"]
    assert agent["guardrails"]["restrictedTopics"] == ["Legal advice"]
    assert agent["modelSettings"]["model"] == "gemini-2.0-flash"


async def test_duplicate_names_are_rejected_within_a_tenant(client: AsyncClient) -> None:
    auth = await headers(client)
    await create(client, auth, name="Twin")

    response = await client.post("/agents", json={"name": "twin"}, headers=auth)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AGENT_NAME_TAKEN"


async def test_two_tenants_may_use_the_same_agent_name(client: AsyncClient) -> None:
    first = await headers(client)
    second = await headers(client)
    await create(client, first, name="Shared Name")

    response = await client.post("/agents", json={"name": "Shared Name"}, headers=second)

    assert response.status_code == 201


async def test_list_is_paginated(client: AsyncClient) -> None:
    auth = await headers(client)
    for index in range(3):
        await create(client, auth, name=f"Agent {index}")

    response = await client.get("/agents?page=1&pageSize=2", headers=auth)

    body = response.json()
    assert response.status_code == 200
    assert len(body["value"]) == 2
    assert body["meta"] == {"page": 1, "pageSize": 2, "totalItems": 3, "totalPages": 2}


async def test_update_increments_the_version(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth, persona="First")

    response = await client.patch(
        f"/agents/{agent['id']}", json={"persona": "Second"}, headers=auth
    )

    body = response.json()["value"]
    assert body["persona"] == "Second"
    assert body["version"] == 2


async def test_an_empty_update_is_a_no_op(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth)

    response = await client.patch(f"/agents/{agent['id']}", json={}, headers=auth)

    assert response.json()["value"]["version"] == 1


async def test_delete_removes_the_agent(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth)

    assert (await client.delete(f"/agents/{agent['id']}", headers=auth)).status_code == 200
    assert (await client.get(f"/agents/{agent['id']}", headers=auth)).status_code == 404


# -- lifecycle -------------------------------------------------------------------


async def test_an_incomplete_agent_cannot_be_published(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth, name="Bare")

    response = await client.post(f"/agents/{agent['id']}/publish", headers=auth)

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "AGENT_NOT_PUBLISHABLE"
    assert "persona is empty" in body["error"]["detail"]
    assert "no model provider selected" in body["error"]["detail"]


async def test_publish_pause_and_return_to_draft(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth, **PUBLISHABLE)
    agent_id = agent["id"]

    published = await client.post(f"/agents/{agent_id}/publish", headers=auth)
    assert published.json()["value"]["status"] == "published"

    paused = await client.post(f"/agents/{agent_id}/pause", headers=auth)
    assert paused.json()["value"]["status"] == "paused"

    republished = await client.post(f"/agents/{agent_id}/publish", headers=auth)
    assert republished.json()["value"]["status"] == "published"

    draft = await client.post(f"/agents/{agent_id}/unpublish", headers=auth)
    assert draft.json()["value"]["status"] == "draft"


async def test_a_draft_cannot_be_paused(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth, **PUBLISHABLE)

    response = await client.post(f"/agents/{agent['id']}/pause", headers=auth)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"


async def test_publishing_twice_is_a_no_op(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth, **PUBLISHABLE)
    await client.post(f"/agents/{agent['id']}/publish", headers=auth)

    again = await client.post(f"/agents/{agent['id']}/publish", headers=auth)

    assert again.status_code == 200
    assert again.json()["value"]["status"] == "published"


# -- versioning ------------------------------------------------------------------


async def test_history_records_the_configuration_before_each_change(
    client: AsyncClient,
) -> None:
    auth = await headers(client)
    agent = await create(client, auth, persona="Original")
    await client.patch(f"/agents/{agent['id']}", json={"persona": "Edited"}, headers=auth)

    versions = (await client.get(f"/agents/{agent['id']}/versions", headers=auth)).json()["value"]

    assert [entry["version"] for entry in versions] == [1]
    assert versions[0]["config"]["persona"] == "Original"


async def test_rollback_restores_an_earlier_configuration(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth, persona="Original")
    agent_id = agent["id"]
    await client.patch(f"/agents/{agent_id}", json={"persona": "Edited"}, headers=auth)

    response = await client.post(
        f"/agents/{agent_id}/versions/1/rollback", json={"note": "undo"}, headers=auth
    )

    body = response.json()["value"]
    assert response.status_code == 200
    assert body["persona"] == "Original"
    assert body["version"] == 3, "a rollback lands as a new version rather than rewinding"


async def test_a_rollback_can_itself_be_rolled_back(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth, persona="Original")
    agent_id = agent["id"]
    await client.patch(f"/agents/{agent_id}", json={"persona": "Edited"}, headers=auth)
    await client.post(f"/agents/{agent_id}/versions/1/rollback", json={}, headers=auth)

    versions = (await client.get(f"/agents/{agent_id}/versions", headers=auth)).json()["value"]
    restored = next(entry for entry in versions if entry["config"]["persona"] == "Edited")
    again = await client.post(
        f"/agents/{agent_id}/versions/{restored['version']}/rollback", json={}, headers=auth
    )

    assert again.json()["value"]["persona"] == "Edited"


async def test_rollback_keeps_a_published_agent_published(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth, **PUBLISHABLE)
    agent_id = agent["id"]
    await client.post(f"/agents/{agent_id}/publish", headers=auth)
    await client.patch(f"/agents/{agent_id}", json={"persona": "Reworded"}, headers=auth)

    response = await client.post(f"/agents/{agent_id}/versions/1/rollback", json={}, headers=auth)

    assert response.json()["value"]["status"] == "published"


async def test_an_unknown_version_is_reported_as_missing(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth)

    response = await client.get(f"/agents/{agent['id']}/versions/99", headers=auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AGENT_VERSION_NOT_FOUND"
