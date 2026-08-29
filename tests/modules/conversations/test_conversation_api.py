"""The conversation endpoints over HTTP (spec §5.1 journey step 3, §5.4).

The provider is replaced through the app's dependency override rather than by patching: the router
builds its own service, and overriding the dependency is the seam FastAPI provides for exactly this.
Everything else — auth, the envelope, tenant scoping — is the real stack.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from src.modules.conversations.domain.services import ConversationService
from src.modules.conversations.presentation.api.conversation_controller import (
    get_conversation_service,
)
from src.modules.tenants.presentation.dependencies import CurrentTenantDep
from src.shared.database.dependencies import SessionDep
from tests.modules.auth.test_auth_flow import auth_header, signup
from tests.modules.conversations.test_turn import RecordingLLM

PUBLISHABLE: dict[str, Any] = {
    "persona": "You are the sales assistant for Nash Paints.",
    "modelProvider": "gemini",
    "modelSettings": {"model": "gemini-2.0-flash", "temperature": 0.5, "maxTokens": 512},
}


@pytest.fixture
def llm() -> RecordingLLM:
    return RecordingLLM()


@pytest.fixture
async def wired(app: FastAPI, llm: RecordingLLM) -> AsyncIterator[FastAPI]:
    """Point the conversation routes at a fake provider."""

    def override(session: SessionDep, tenant_id: CurrentTenantDep) -> ConversationService:
        return ConversationService(session, tenant_id, llm_client=llm)  # type: ignore[arg-type]

    app.dependency_overrides[get_conversation_service] = override
    yield app
    app.dependency_overrides.pop(get_conversation_service, None)


async def headers(client: AsyncClient) -> dict[str, str]:
    return auth_header((await signup(client))["tokens"])


async def published_agent(
    client: AsyncClient, auth: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    payload = {"name": f"Agent {uuid.uuid4().hex[:6]}", **PUBLISHABLE, **overrides}
    created = await client.post("/agents", json=payload, headers=auth)
    assert created.status_code == 201, created.text
    agent: dict[str, Any] = created.json()["value"]
    published = await client.post(f"/agents/{agent['id']}/publish", headers=auth)
    assert published.status_code == 200, published.text
    return agent


async def send(
    client: AsyncClient, auth: dict[str, str], agent_id: str, message: str, **extra: Any
) -> tuple[int, dict[str, Any]]:
    response = await client.post(
        "/conversations/messages",
        json={"agentId": agent_id, "message": message, **extra},
        headers=auth,
    )
    return response.status_code, response.json()


# -- the preview turn ------------------------------------------------------------------


async def test_a_turn_returns_the_reply_and_how_it_was_produced(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)

    status, body = await send(client, auth, agent["id"], "Can I return tinted paint?")

    assert status == 201, body
    value = body["value"]
    assert value["reply"]["content"] == "Tinted paint is final sale, I'm afraid."
    assert value["reply"]["role"] == "assistant"
    assert value["status"] == "active"
    assert value["escalated"] is False
    assert value["usedKnowledge"] is False, "this agent has no knowledge attached"


async def test_a_draft_agent_can_be_previewed(client: AsyncClient, wired: FastAPI) -> None:
    """Journey step 3: testing before publishing is what the preview chat is for."""
    auth = await headers(client)
    created = await client.post(
        "/agents", json={"name": "Draft agent", **PUBLISHABLE}, headers=auth
    )
    agent = created.json()["value"]

    status, body = await send(client, auth, agent["id"], "Hello")

    assert status == 201, body


async def test_an_agent_with_no_model_is_refused(client: AsyncClient, wired: FastAPI) -> None:
    auth = await headers(client)
    created = await client.post("/agents", json={"name": "Bare agent"}, headers=auth)

    status, body = await send(client, auth, created.json()["value"]["id"], "Hello")

    assert status == 422
    assert body["error"]["code"] == "AGENT_NOT_CONFIGURED"


async def test_an_empty_message_is_rejected(client: AsyncClient, wired: FastAPI) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)

    status, _ = await send(client, auth, agent["id"], "   ")

    assert status == 422


# -- sessions and transcripts ------------------------------------------------------------


async def test_the_session_continues_across_turns(client: AsyncClient, wired: FastAPI) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)

    _, first = await send(client, auth, agent["id"], "Hello", externalUserId="ada")
    _, second = await send(client, auth, agent["id"], "Still there?", externalUserId="ada")

    assert first["value"]["conversationId"] == second["value"]["conversationId"]


async def test_the_transcript_reads_in_order_with_its_costs(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)
    _, turn = await send(client, auth, agent["id"], "Hello", externalUserId="ada")
    conversation_id = turn["value"]["conversationId"]

    body = (await client.get(f"/conversations/{conversation_id}/messages", headers=auth)).json()

    assert [message["role"] for message in body["value"]] == ["user", "assistant"]
    assert body["value"][0]["content"] == "Hello"
    assert body["value"][1]["promptTokens"] == 420


async def test_the_detail_view_totals_tokens_and_cost(
    client: AsyncClient, wired: FastAPI, config_override: Callable[..., None]
) -> None:
    config_override(LLM_PRICE_TABLE="gemini-2.0-flash=1/2")
    auth = await headers(client)
    agent = await published_agent(client, auth)
    _, turn = await send(client, auth, agent["id"], "Hello", externalUserId="ada")

    body = (
        await client.get(f"/conversations/{turn['value']['conversationId']}", headers=auth)
    ).json()

    assert body["value"]["usage"]["totalTokens"] == 455
    assert body["value"]["usage"]["costMicroUsd"] == 490
    assert body["value"]["conversation"]["channel"] == "preview"


async def test_conversations_can_be_listed_and_filtered(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await headers(client)
    first = await published_agent(client, auth)
    second = await published_agent(client, auth)
    await send(client, auth, first["id"], "Hello", externalUserId="ada")
    await send(client, auth, second["id"], "Hello", externalUserId="grace")

    everything = (await client.get("/conversations", headers=auth)).json()
    filtered = (await client.get(f"/conversations?agentId={first['id']}", headers=auth)).json()

    assert everything["meta"]["totalItems"] == 2
    assert filtered["meta"]["totalItems"] == 1
    assert filtered["value"][0]["agentId"] == first["id"]


# -- guardrails and lifecycle --------------------------------------------------------------


async def test_an_escalation_is_reported_on_the_turn(
    client: AsyncClient, wired: FastAPI, llm: RecordingLLM
) -> None:
    auth = await headers(client)
    agent = await published_agent(
        client, auth, engagementRules={"escalationTriggers": ["speak to a manager"]}
    )

    _, body = await send(client, auth, agent["id"], "Please let me speak to a manager")

    assert body["value"]["escalated"] is True
    assert body["value"]["status"] == "escalated"
    assert llm.requests == [], "a handoff does not pay for a model call"


async def test_a_restricted_topic_is_declined_with_the_configured_fallback(
    client: AsyncClient, wired: FastAPI, llm: RecordingLLM
) -> None:
    auth = await headers(client)
    agent = await published_agent(
        client,
        auth,
        guardrails={
            "restrictedTopics": ["legal advice"],
            "fallbackResponse": "I can't advise on that, sorry.",
        },
    )

    _, body = await send(client, auth, agent["id"], "Can you give me legal advice?")

    assert body["value"]["reply"]["content"] == "I can't advise on that, sorry."
    assert llm.requests == []


async def test_a_conversation_can_be_escalated_by_the_tenant(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)
    _, turn = await send(client, auth, agent["id"], "Hello", externalUserId="ada")
    conversation_id = turn["value"]["conversationId"]

    response = await client.post(
        f"/conversations/{conversation_id}/escalate",
        json={"reason": "Customer asked for a manager."},
        headers=auth,
    )

    assert response.status_code == 200
    assert response.json()["value"]["status"] == "escalated"
    assert response.json()["value"]["escalationReason"] == "Customer asked for a manager."


async def test_a_closed_conversation_cannot_be_continued_by_id(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)
    _, turn = await send(client, auth, agent["id"], "Hello", externalUserId="ada")
    conversation_id = turn["value"]["conversationId"]
    await client.post(f"/conversations/{conversation_id}/close", headers=auth)

    status, body = await send(client, auth, agent["id"], "More", conversationId=conversation_id)

    assert status == 409
    assert body["error"]["code"] == "CONVERSATION_NOT_ACTIVE"


# -- isolation --------------------------------------------------------------------------------


async def test_another_tenants_conversation_is_reported_as_missing(
    client: AsyncClient, wired: FastAPI
) -> None:
    first = await headers(client)
    second = await headers(client)
    agent = await published_agent(client, first)
    _, turn = await send(client, first, agent["id"], "Our margin is 42 percent")
    conversation_id = turn["value"]["conversationId"]

    for path in ("", "/messages"):
        response = await client.get(f"/conversations/{conversation_id}{path}", headers=second)
        assert response.status_code == 404
        assert "42 percent" not in response.text


async def test_another_tenants_agent_cannot_be_talked_to(
    client: AsyncClient, wired: FastAPI
) -> None:
    first = await headers(client)
    second = await headers(client)
    agent = await published_agent(client, first)

    status, body = await send(client, second, agent["id"], "Hello")

    assert status == 404
    assert body["error"]["code"] == "AGENT_NOT_FOUND"


async def test_another_tenants_conversation_cannot_be_escalated_or_closed(
    client: AsyncClient, wired: FastAPI
) -> None:
    first = await headers(client)
    second = await headers(client)
    agent = await published_agent(client, first)
    _, turn = await send(client, first, agent["id"], "Hello")
    conversation_id = turn["value"]["conversationId"]

    for suffix in ("escalate", "close"):
        response = await client.post(
            f"/conversations/{conversation_id}/{suffix}", json={}, headers=second
        )
        assert response.status_code == 404

    still_active = await client.get(f"/conversations/{conversation_id}", headers=first)
    assert still_active.json()["value"]["conversation"]["status"] == "active"


async def test_conversation_routes_require_authentication(client: AsyncClient) -> None:
    unknown = uuid.uuid4()

    assert (await client.get("/conversations")).status_code == 401
    assert (await client.get(f"/conversations/{unknown}")).status_code == 401
    assert (
        await client.post("/conversations/messages", json={"agentId": str(unknown), "message": "x"})
    ).status_code == 401
