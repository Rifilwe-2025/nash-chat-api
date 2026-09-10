"""The public chat API, held by an external caller with only a key (spec §5.5, §5.6).

This is the phase's bar: someone holding nothing but an API key can hold a conversation with a
published agent, a revoked key stops working immediately, and the limit returns 429 with the right
headers.

Every request here goes through the real authentication path — no user token, no tenant in the URL.
The provider is overridden because that is the only thing these tests should not be exercising.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette.types import Message

from src import configs
from src.core.rate_limit import InMemoryBackend, RateLimiter
from src.modules.channels.domain.models import WebhookEvent
from src.modules.channels.domain.services import ChannelService
from src.modules.channels.web.presentation.dependencies import (
    ApiCallerDep,
    get_chat_conversations,
)
from src.modules.conversations.domain.models import Conversation
from src.modules.conversations.domain.services import ConversationService
from src.shared.database.dependencies import SessionDep
from tests.modules.auth.test_auth_flow import auth_header, signup
from tests.modules.conversations.test_turn import InterruptedLLM, LeavingLLM, RecordingLLM

PUBLISHABLE: dict[str, Any] = {
    "persona": "You are the sales assistant for Nash Paints.",
    "modelProvider": "gemini",
    "modelSettings": {"model": "gemini-2.0-flash", "temperature": 0.5, "maxTokens": 512},
}


@pytest.fixture
def llm() -> RecordingLLM:
    return RecordingLLM()


def serve_with(app: FastAPI, llm: RecordingLLM) -> None:
    """Answer the public chat routes with ``llm``, replacing whichever fake was there."""

    def override(session: SessionDep, caller: ApiCallerDep) -> ConversationService:
        return ConversationService(session, caller.tenant_id, llm_client=llm)  # type: ignore[arg-type]

    app.dependency_overrides[get_chat_conversations] = override


@pytest.fixture
async def wired(app: FastAPI, llm: RecordingLLM) -> AsyncIterator[FastAPI]:
    """Point the public chat routes at a fake provider, and give each test a fresh limiter.

    The limiter is replaced per test so counts from one cannot refuse a request in the next.
    """
    serve_with(app, llm)
    app.state.rate_limiter = RateLimiter(InMemoryBackend())
    try:
        yield app
    finally:
        app.dependency_overrides.pop(get_chat_conversations, None)


async def owner(client: AsyncClient) -> dict[str, str]:
    return auth_header((await signup(client))["tokens"])


async def agent_with_key(
    client: AsyncClient, auth: dict[str, str], publish: bool = True, **key_payload: Any
) -> tuple[dict[str, Any], str, str]:
    """Returns ``(agent, secret, key id)`` for an agent ready to serve traffic."""
    created = await client.post(
        "/agents", json={"name": f"Agent {uuid.uuid4().hex[:6]}", **PUBLISHABLE}, headers=auth
    )
    agent: dict[str, Any] = created.json()["value"]
    if publish:
        await client.post(f"/agents/{agent['id']}/publish", headers=auth)

    issued = await client.post(
        f"/api-keys?agentId={agent['id']}",
        json={"name": "Widget", **key_payload},
        headers=auth,
    )
    assert issued.status_code == 201, issued.text
    value = issued.json()["value"]
    return agent, value["key"], value["apiKey"]["id"]


def key_header(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


async def send(
    client: AsyncClient, secret: str, message: str = "Do you deliver to Bulawayo?", **extra: Any
) -> Any:
    return await client.post(
        "/v1/chat/messages",
        json={"message": message, "userId": "visitor-1", **extra},
        headers=key_header(secret),
    )


# -- the bar ---------------------------------------------------------------------------


async def test_a_caller_with_only_a_key_can_hold_a_conversation(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth)

    first = await send(client, secret, "Do you deliver to Bulawayo?")
    second = await send(client, secret, "And how much is it?")

    assert first.status_code == 201, first.text
    assert first.json()["value"]["reply"]
    assert first.json()["value"]["escalated"] is False
    assert second.json()["value"]["conversationId"] == first.json()["value"]["conversationId"]


async def test_the_key_header_is_accepted_as_well_as_bearer(
    client: AsyncClient, wired: FastAPI
) -> None:
    """Integrations reach for one or the other; refusing the wrong one teaches nothing."""
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth)

    response = await client.post(
        "/v1/chat/messages",
        json={"message": "Hello", "userId": "visitor-1"},
        headers={"X-API-Key": secret},
    )

    assert response.status_code == 201


async def test_a_revoked_key_is_refused_immediately(client: AsyncClient, wired: FastAPI) -> None:
    """No cached decision, so there is no window in which a revoked key still works."""
    auth = await owner(client)
    _, secret, key_id = await agent_with_key(client, auth)
    assert (await send(client, secret)).status_code == 201

    await client.post(f"/api-keys/{key_id}/revoke", headers=auth)

    refused = await send(client, secret)
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "INVALID_API_KEY"


async def test_the_rate_limit_returns_429_with_the_right_headers(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth, rateLimitPerMinute=2)

    first = await send(client, secret)
    second = await send(client, secret)
    third = await send(client, secret)

    assert [first.status_code, second.status_code] == [201, 201]
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "RATE_LIMITED"
    assert third.headers["Retry-After"]
    assert third.headers["X-RateLimit-Limit"] == "2"
    assert third.headers["X-RateLimit-Remaining"] == "0"


async def test_every_allowed_response_reports_the_remaining_allowance(
    client: AsyncClient, wired: FastAPI
) -> None:
    """A client should learn where it stands from a success, not only from being refused."""
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth, rateLimitPerMinute=10)

    response = await send(client, secret)

    assert response.headers["X-RateLimit-Limit"] == "10"
    assert response.headers["X-RateLimit-Remaining"] == "9"
    assert response.headers["X-RateLimit-Reset"]


# -- authentication failures read alike --------------------------------------------------


async def test_a_missing_key_is_refused(client: AsyncClient, wired: FastAPI) -> None:
    response = await client.post(
        "/v1/chat/messages", json={"message": "Hello", "userId": "visitor-1"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "MISSING_API_KEY"


async def test_an_unknown_key_is_refused(client: AsyncClient, wired: FastAPI) -> None:
    response = await send(client, "nsk_live_not_a_real_key_at_all")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_API_KEY"


async def test_an_unpublished_agent_does_not_serve_traffic(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth, publish=False)

    response = await send(client, secret)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AGENT_NOT_PUBLISHED"


async def test_pausing_an_agent_stops_its_keys_working(client: AsyncClient, wired: FastAPI) -> None:
    auth = await owner(client)
    agent, secret, _ = await agent_with_key(client, auth)
    assert (await send(client, secret)).status_code == 201

    await client.post(f"/agents/{agent['id']}/pause", headers=auth)

    assert (await send(client, secret)).status_code == 403


# -- scopes -------------------------------------------------------------------------------


async def test_a_write_only_key_cannot_read_history(client: AsyncClient, wired: FastAPI) -> None:
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth, scopes=["chat:write"])
    sent = await send(client, secret)
    conversation_id = sent.json()["value"]["conversationId"]

    response = await client.get(
        f"/v1/chat/conversations/{conversation_id}/messages", headers=key_header(secret)
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_SCOPE"


async def test_a_read_only_key_cannot_send(client: AsyncClient, wired: FastAPI) -> None:
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth, scopes=["chat:read"])

    response = await send(client, secret)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_SCOPE"


# -- history and sessions ------------------------------------------------------------------


async def test_history_can_be_read_back(client: AsyncClient, wired: FastAPI) -> None:
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth)
    sent = await send(client, secret, "Do you deliver to Bulawayo?")
    conversation_id = sent.json()["value"]["conversationId"]

    response = await client.get(
        f"/v1/chat/conversations/{conversation_id}/messages", headers=key_header(secret)
    )

    assert response.status_code == 200
    roles = [message["role"] for message in response.json()["value"]]
    assert roles == ["user", "assistant"]
    assert "promptTokens" not in response.json()["value"][0], "internal detail stays internal"


async def test_the_open_session_can_be_looked_up(client: AsyncClient, wired: FastAPI) -> None:
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth)
    sent = await send(client, secret)

    response = await client.get("/v1/chat/session?userId=visitor-1", headers=key_header(secret))

    assert response.json()["value"]["conversationId"] == sent.json()["value"]["conversationId"]


async def test_an_unknown_user_has_no_open_session(client: AsyncClient, wired: FastAPI) -> None:
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth)

    response = await client.get("/v1/chat/session?userId=nobody", headers=key_header(secret))

    assert response.status_code == 200
    assert "conversationId" not in response.json()["value"]


async def test_a_key_cannot_read_another_agents_conversation(
    client: AsyncClient, wired: FastAPI
) -> None:
    """A key speaks for one agent, even within its own tenant."""
    auth = await owner(client)
    _, first_secret, _ = await agent_with_key(client, auth)
    _, second_secret, _ = await agent_with_key(client, auth)
    sent = await send(client, first_secret)
    conversation_id = sent.json()["value"]["conversationId"]

    response = await client.get(
        f"/v1/chat/conversations/{conversation_id}/messages", headers=key_header(second_secret)
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


# -- streaming -------------------------------------------------------------------------------


async def test_a_streamed_reply_arrives_as_sse_frames(client: AsyncClient, wired: FastAPI) -> None:
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth)

    response = await client.post(
        "/v1/chat/messages/stream",
        json={"message": "Hello", "userId": "visitor-1"},
        headers=key_header(secret),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["X-Conversation-Id"]
    assert "event: delta" in response.text
    assert "event: done" in response.text


async def test_a_streamed_turn_is_still_stored(client: AsyncClient, wired: FastAPI) -> None:
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth)

    streamed = await client.post(
        "/v1/chat/messages/stream",
        json={"message": "Hello there", "userId": "visitor-1"},
        headers=key_header(secret),
    )
    conversation_id = streamed.headers["X-Conversation-Id"]

    history = await client.get(
        f"/v1/chat/conversations/{conversation_id}/messages", headers=key_header(secret)
    )
    contents = [message["content"] for message in history.json()["value"]]
    assert "Hello there" in contents
    assert len(contents) == 2


# -- guardrails on the public surface -----------------------------------------------------------


async def test_an_escalation_is_reported_to_the_integration(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await owner(client)
    created = await client.post(
        "/agents",
        json={
            "name": f"Agent {uuid.uuid4().hex[:6]}",
            **PUBLISHABLE,
            "engagementRules": {"escalationTriggers": ["speak to a manager"]},
        },
        headers=auth,
    )
    agent = created.json()["value"]
    await client.post(f"/agents/{agent['id']}/publish", headers=auth)
    issued = await client.post(
        f"/api-keys?agentId={agent['id']}", json={"name": "Widget"}, headers=auth
    )
    secret = issued.json()["value"]["key"]

    response = await send(client, secret, "Please let me speak to a manager")

    assert response.json()["value"]["escalated"] is True


async def test_an_empty_message_is_rejected(client: AsyncClient, wired: FastAPI) -> None:
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth)

    response = await send(client, secret, "   ")

    assert response.status_code == 422


async def test_rate_limit_counting_is_per_key_not_per_agent(
    client: AsyncClient, wired: FastAPI, config_override: Callable[..., None]
) -> None:
    """Two integrations on one agent should not consume each other's allowance."""
    auth = await owner(client)
    created = await client.post(
        "/agents", json={"name": f"Agent {uuid.uuid4().hex[:6]}", **PUBLISHABLE}, headers=auth
    )
    agent = created.json()["value"]
    await client.post(f"/agents/{agent['id']}/publish", headers=auth)

    secrets = []
    for name in ("Widget", "Mobile"):
        issued = await client.post(
            f"/api-keys?agentId={agent['id']}",
            json={"name": name, "rateLimitPerMinute": 1},
            headers=auth,
        )
        secrets.append(issued.json()["value"]["key"])

    assert (await send(client, secrets[0])).status_code == 201
    assert (await send(client, secrets[0])).status_code == 429
    assert (await send(client, secrets[1])).status_code == 201


# -- how a stream ends ---------------------------------------------------------------------------


def frames_of(body: str) -> list[tuple[str, dict[str, Any]]]:
    """An SSE body as ``(event, payload)`` pairs, in the order they were sent."""
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in body.strip().split("\n\n"):
        lines = block.split("\n")
        event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = "\n".join(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        frames.append((event, json.loads(data)))
    return frames


async def send_streamed(
    client: AsyncClient, secret: str, message: str = "Hello", **headers: str
) -> Any:
    return await client.post(
        "/v1/chat/messages/stream",
        json={"message": message, "userId": "visitor-1"},
        headers={**key_header(secret), **headers},
    )


async def escalating_agent_key(client: AsyncClient, auth: dict[str, str]) -> str:
    created = await client.post(
        "/agents",
        json={
            "name": f"Agent {uuid.uuid4().hex[:6]}",
            **PUBLISHABLE,
            "engagementRules": {"escalationTriggers": ["speak to a manager"]},
        },
        headers=auth,
    )
    agent = created.json()["value"]
    await client.post(f"/agents/{agent['id']}/publish", headers=auth)
    issued = await client.post(
        f"/api-keys?agentId={agent['id']}", json={"name": "Widget"}, headers=auth
    )
    return str(issued.json()["value"]["key"])


async def test_a_finished_stream_ends_with_the_conversation_and_its_escalation_state(
    client: AsyncClient, wired: FastAPI, llm: RecordingLLM
) -> None:
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth)

    response = await send_streamed(client, secret)

    frames = frames_of(response.text)
    assert frames[-1] == (
        "done",
        {"done": True, "conversationId": response.headers["X-Conversation-Id"], "escalated": False},
    )
    assert "".join(payload["delta"] for event, payload in frames if event == "delta") == llm.reply


async def test_a_streamed_escalation_is_reported_in_the_done_frame(
    client: AsyncClient, wired: FastAPI
) -> None:
    """Only the buffered reply used to say so, so a streaming widget kept talking to nobody."""
    auth = await owner(client)
    secret = await escalating_agent_key(client, auth)

    response = await send_streamed(client, secret, "Please let me speak to a manager")

    event, done = frames_of(response.text)[-1]
    assert event == "done"
    assert done["escalated"] is True
    session = await client.get("/v1/chat/session?userId=visitor-1", headers=key_header(secret))
    assert "conversationId" not in session.json()["value"], "it is no longer the open session"


async def test_a_model_failing_mid_stream_ends_in_an_error_frame_not_in_the_reply(
    client: AsyncClient, app: FastAPI, wired: FastAPI
) -> None:
    """The failure notice used to be streamed as reply text, which no client could tell apart."""
    serve_with(app, InterruptedLLM())
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth)

    response = await send_streamed(client, secret)

    frames = frames_of(response.text)
    assert [event for event, _ in frames] == ["delta", "error"]
    assert frames[0][1] == {"delta": "Tinted paint is"}
    assert frames[1][1]["code"] == "PROVIDER_UNAVAILABLE"

    history = await client.get(
        f"/v1/chat/conversations/{response.headers['X-Conversation-Id']}/messages",
        headers=key_header(secret),
    )
    contents = [message["content"] for message in history.json()["value"]]
    assert contents == ["Hello", "Tinted paint is"], "what was shown is kept"


async def test_a_streamed_conversation_emits_the_webhooks_a_sent_one_does(
    client: AsyncClient, wired: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streamed turns used to bypass the channel layer, so no subscriber ever heard about them."""
    emitted: list[tuple[WebhookEvent, uuid.UUID]] = []

    async def record(self: ChannelService, event: WebhookEvent, conversation: Conversation) -> None:
        emitted.append((event, conversation.id))

    monkeypatch.setattr(ChannelService, "emit", record)
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth)
    escalating = await escalating_agent_key(client, auth)

    first = await send_streamed(client, secret)
    await send_streamed(client, secret, "And another thing")
    handed_over = await send_streamed(client, escalating, "Please let me speak to a manager")

    started = uuid.UUID(first.headers["X-Conversation-Id"])
    escalated = uuid.UUID(handed_over.headers["X-Conversation-Id"])
    assert emitted == [
        (WebhookEvent.CONVERSATION_STARTED, started),
        (WebhookEvent.CONVERSATION_STARTED, escalated),
        (WebhookEvent.CONVERSATION_ESCALATED, escalated),
    ]


# -- a visitor leaving mid-stream ----------------------------------------------------------------


async def stream_and_leave(app: FastAPI, secret: str, spec_version: str) -> str:
    """Stream a reply over raw ASGI, and walk away once the first frame has arrived.

    ``2.3`` is how uvicorn describes a connection today, and Starlette cancels the stream when the
    client goes. ``2.4`` is the newer contract, where the failed write raises out of the response
    instead. Both must leave the turn stored. Returns the conversation id.
    """
    body = json.dumps({"message": "Hello there", "userId": "visitor-leaves"}).encode()
    first_frame_sent = anyio.Event()
    body_delivered = False
    conversation_id = ""

    async def receive() -> Message:
        nonlocal body_delivered
        if not body_delivered:
            body_delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await first_frame_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        nonlocal conversation_id
        if message["type"] == "http.response.start":
            headers = {name.decode().lower(): value.decode() for name, value in message["headers"]}
            conversation_id = headers["x-conversation-id"]
        elif message["type"] == "http.response.body" and message.get("body"):
            if first_frame_sent.is_set() and spec_version == "2.4":
                raise OSError("the visitor closed the tab")
            first_frame_sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 50000),
        "root_path": "",
        "path": "/v1/chat/messages/stream",
        "raw_path": b"/v1/chat/messages/stream",
        "query_string": b"",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {secret}".encode()),
        ],
    }
    with anyio.fail_after(10):
        await app(scope, receive, send)
    return conversation_id


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_a_visitor_who_leaves_mid_stream_keeps_their_turn(
    client: AsyncClient, app: FastAPI, wired: FastAPI, spec_version: str
) -> None:
    """Both what they sent and what they had been shown are stored — neither is rolled back."""
    serve_with(app, LeavingLLM())
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth)

    conversation_id = await stream_and_leave(app, secret, spec_version)

    history = await client.get(
        f"/v1/chat/conversations/{conversation_id}/messages", headers=key_header(secret)
    )
    contents = [message["content"] for message in history.json()["value"]]
    assert contents[0] == "Hello there"
    assert len(contents) == 2
    assert contents[1].startswith("Tinted")


# -- browser origins -----------------------------------------------------------------------------

SITE = "https://shop.example.com"


async def allow_only(
    client: AsyncClient, auth: dict[str, str], agent_id: str, *origins: str
) -> Any:
    return await client.put(
        f"/agents/{agent_id}/channels/web",
        json={"settings": {"allowedOrigins": list(origins)}},
        headers=auth,
    )


async def send_from(client: AsyncClient, secret: str, origin: str | None) -> Any:
    headers = key_header(secret)
    if origin is not None:
        headers["Origin"] = origin
    return await client.post(
        "/v1/chat/messages",
        json={"message": "Do you deliver to Bulawayo?", "userId": "visitor-1"},
        headers=headers,
    )


async def test_an_agent_with_no_allowlist_answers_any_browser_origin(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await owner(client)
    _, secret, _ = await agent_with_key(client, auth)

    assert (await send_from(client, secret, SITE)).status_code == 201


async def test_an_allowlist_refuses_browsers_on_any_other_origin(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await owner(client)
    agent, secret, _ = await agent_with_key(client, auth)
    assert (await allow_only(client, auth, agent["id"], SITE)).status_code == 200

    refused = await send_from(client, secret, "https://elsewhere.example")

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
    assert (await send_from(client, secret, SITE)).status_code == 201


async def test_a_server_without_an_origin_is_not_affected_by_the_allowlist(
    client: AsyncClient, wired: FastAPI
) -> None:
    """The allowlist governs embedding. The key is the credential, and a server has no Origin."""
    auth = await owner(client)
    agent, secret, _ = await agent_with_key(client, auth)
    await allow_only(client, auth, agent["id"], SITE)

    assert (await send_from(client, secret, None)).status_code == 201


async def test_the_console_can_still_test_an_agent_locked_to_a_site(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await owner(client)
    agent, secret, _ = await agent_with_key(client, auth)
    await allow_only(client, auth, agent["id"], SITE)

    assert (await send_from(client, secret, configs.CORS_ALLOW_ORIGINS[0])).status_code == 201


async def test_the_allowlist_covers_reads_and_streams_too(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await owner(client)
    agent, secret, _ = await agent_with_key(client, auth)
    await allow_only(client, auth, agent["id"], SITE)
    elsewhere = {"Origin": "https://elsewhere.example"}

    session = await client.get(
        "/v1/chat/session?userId=visitor-1", headers={**key_header(secret), **elsewhere}
    )
    stream = await send_streamed(client, secret, **elsewhere)

    assert session.status_code == 403
    assert stream.status_code == 403
    assert stream.headers["content-type"].startswith("application/json"), "refused before a stream"


async def test_allowed_origins_are_stored_in_the_shape_a_browser_sends(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await owner(client)
    agent, _, _ = await agent_with_key(client, auth)

    response = await allow_only(client, auth, agent["id"], "HTTPS://Shop.Example.com:443/", SITE)

    assert response.status_code == 200
    assert response.json()["value"]["settings"]["allowedOrigins"] == [SITE]


async def test_an_allowed_origin_with_a_path_is_refused_rather_than_trimmed(
    client: AsyncClient, wired: FastAPI
) -> None:
    """``https://example.com/shop`` would suggest a restriction to a path that does not exist."""
    auth = await owner(client)
    agent, _, _ = await agent_with_key(client, auth)

    response = await allow_only(client, auth, agent["id"], "https://example.com/shop")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_ORIGIN"
