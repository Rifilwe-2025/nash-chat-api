"""Per-agent provider credentials: storing one, hiding it, testing it, removing it.

The credential is the one field on an agent that must never come back out, so a good deal of what
is asserted here is an absence — the key is not in the agent, not in the list, not in the version
history, and not readable in the column.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.agents.domain.models import Agent
from src.shared.llm import registry
from src.shared.llm.base import CompletionRequest, CompletionResult, LLMProvider, TokenUsage
from src.shared.llm.errors import LLMAuthenticationError, LLMError
from tests.modules.agents.test_agent_crud import create, headers

KEY = "sk-live-abcdefghijklmnop-a91f"
CONFIGURED: dict[str, Any] = {
    "modelProvider": "gemini",
    "modelSettings": {"model": "gemini-2.0-flash"},
}
# 32 bytes, base64 — the shape `SECURITY_ENCRYPTION_KEY` requires.
ENCRYPTION_KEY = "bmFzaC10ZXN0LWVuY3J5cHRpb24ta2V5LTMyLWJ5dGU="


class StubProvider(LLMProvider):
    """Answers the probe, or rejects it the way the test asked."""

    name = "gemini"

    def __init__(self, api_key: str | None = None, error: LLMError | None = None) -> None:
        self.api_key = api_key
        self.error = error

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        if self.error is not None:
            raise self.error
        return CompletionResult(
            content="ok",
            usage=TokenUsage(prompt_tokens=4, completion_tokens=1),
            model=request.model,
            provider=self.name,
        )

    def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        raise NotImplementedError("a probe never streams")


class Provider:
    """The registered stub, recording which key each probe was built with."""

    def __init__(self) -> None:
        self.keys: list[str | None] = []
        self.error: LLMError | None = None

    def build(self, api_key: str | None) -> LLMProvider:
        self.keys.append(api_key)
        return StubProvider(api_key=api_key, error=self.error)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Provider:
    """Replace the real Gemini factory. Nothing in this module reaches a network."""
    stub = Provider()
    monkeypatch.setitem(registry.PROVIDERS, "gemini", stub.build)
    return stub


async def stored_key(session: AsyncSession, agent_id: str) -> str | None:
    """The key as the application sees it — read through the ORM, which decrypts."""
    agent = await session.scalar(select(Agent).where(Agent.id == uuid.UUID(agent_id)))
    assert agent is not None
    await session.refresh(agent)
    return agent.model_api_key


def serialised(payload: Any) -> str:
    """The whole response as one string, for asserting a secret is nowhere inside it."""
    return json.dumps(payload)


# -- storing ---------------------------------------------------------------------


async def test_a_key_can_be_set_at_creation_and_is_never_echoed(client: AsyncClient) -> None:
    auth = await headers(client)

    agent = await create(client, auth, name="Keyed", **CONFIGURED, modelApiKey=KEY)

    assert agent["hasModelApiKey"] is True
    assert agent["modelApiKeyHint"] == "…a91f"
    assert KEY not in serialised(agent)


async def test_a_key_can_be_added_later_by_patch(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth, name="Later", **CONFIGURED)
    assert agent["hasModelApiKey"] is False

    response = await client.patch(f"/agents/{agent['id']}", json={"modelApiKey": KEY}, headers=auth)

    assert response.status_code == 200, response.text
    updated = response.json()["value"]
    assert updated["hasModelApiKey"] is True
    assert KEY not in serialised(updated)


async def test_setting_a_key_does_not_create_a_version(client: AsyncClient) -> None:
    """A credential is not configuration history, so rotating one is not an edit to roll back."""
    auth = await headers(client)
    agent = await create(client, auth, name="Unversioned", **CONFIGURED)

    await client.patch(f"/agents/{agent['id']}", json={"modelApiKey": KEY}, headers=auth)

    current = (await client.get(f"/agents/{agent['id']}", headers=auth)).json()["value"]
    assert current["version"] == agent["version"], "the version must not move for a key alone"

    versions = (await client.get(f"/agents/{agent['id']}/versions", headers=auth)).json()["value"]
    assert versions == []


async def test_a_key_never_reaches_the_version_snapshot(client: AsyncClient) -> None:
    """Snapshots are plaintext JSONB that history never deletes. A key in one lives forever."""
    auth = await headers(client)
    agent = await create(client, auth, name="Snapshotted", **CONFIGURED, modelApiKey=KEY)

    await client.patch(f"/agents/{agent['id']}", json={"persona": "Changed."}, headers=auth)

    versions = (await client.get(f"/agents/{agent['id']}/versions", headers=auth)).json()["value"]
    assert versions, "the edit should have snapshotted the previous configuration"
    assert KEY not in serialised(versions)


async def test_a_rollback_leaves_the_key_alone(client: AsyncClient, session: AsyncSession) -> None:
    """Restoring an old persona must not restore, or wipe, the credential serving traffic today."""
    auth = await headers(client)
    agent = await create(client, auth, name="Rolled", **CONFIGURED, modelApiKey=KEY)
    await client.patch(f"/agents/{agent['id']}", json={"persona": "Second."}, headers=auth)

    response = await client.post(
        f"/agents/{agent['id']}/versions/1/rollback", json={}, headers=auth
    )

    assert response.status_code == 200, response.text
    assert response.json()["value"]["hasModelApiKey"] is True
    assert await stored_key(session, agent["id"]) == KEY


async def test_a_blank_key_is_rejected_rather_than_stored(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth, name="Blank", **CONFIGURED, modelApiKey=KEY)

    response = await client.patch(
        f"/agents/{agent['id']}", json={"modelApiKey": "   "}, headers=auth
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_API_KEY_EMPTY"


async def test_a_pasted_key_is_trimmed(client: AsyncClient, session: AsyncSession) -> None:
    """A copied credential arrives with a newline more often than not, and every provider 401s."""
    auth = await headers(client)

    agent = await create(client, auth, name="Trimmed", **CONFIGURED, modelApiKey=f"  {KEY}\n")

    assert await stored_key(session, agent["id"]) == KEY


async def test_the_key_is_encrypted_in_the_column(
    client: AsyncClient, session: AsyncSession, config_override: Callable[..., None]
) -> None:
    """With a key configured, a database dump must not show the credential."""
    config_override(SECURITY_ENCRYPTION_KEY=ENCRYPTION_KEY)
    auth = await headers(client)

    agent = await create(client, auth, name="Sealed", **CONFIGURED, modelApiKey=KEY)

    # Raw SQL on purpose: reading the ORM column would decrypt it and prove nothing.
    raw = await session.scalar(
        text("SELECT model_api_key FROM agent WHERE id = :id"), {"id": agent["id"]}
    )
    assert raw is not None
    assert raw.startswith("v1:"), "stored as the versioned envelope, not in clear"
    assert KEY not in raw
    assert await stored_key(session, agent["id"]) == KEY, "and it still reads back"


# -- listing ---------------------------------------------------------------------


async def test_the_list_says_which_agents_carry_their_own_key(client: AsyncClient) -> None:
    auth = await headers(client)
    await create(client, auth, name="With", **CONFIGURED, modelApiKey=KEY)
    await create(client, auth, name="Without", **CONFIGURED)

    listed = (await client.get("/agents", headers=auth)).json()["value"]

    by_name = {agent["name"]: agent for agent in listed}
    assert by_name["With"]["hasModelApiKey"] is True
    assert by_name["Without"]["hasModelApiKey"] is False
    assert KEY not in serialised(listed)


# -- removing --------------------------------------------------------------------


async def test_a_key_can_be_removed(client: AsyncClient, session: AsyncSession) -> None:
    auth = await headers(client)
    agent = await create(client, auth, name="Removable", **CONFIGURED, modelApiKey=KEY)

    response = await client.delete(f"/agents/{agent['id']}/model-key", headers=auth)

    assert response.status_code == 200, response.text
    assert response.json()["value"]["hasModelApiKey"] is False
    assert await stored_key(session, agent["id"]) is None


async def test_removing_a_key_that_is_not_there_is_a_no_op(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth, name="Bare", **CONFIGURED)

    response = await client.delete(f"/agents/{agent['id']}/model-key", headers=auth)

    assert response.status_code == 200
    assert response.json()["value"]["hasModelApiKey"] is False


# -- testing ---------------------------------------------------------------------


async def test_a_standalone_check_reports_a_working_key(
    client: AsyncClient, provider: Provider
) -> None:
    auth = await headers(client)

    response = await client.post(
        "/agents/model-key/test",
        json={"modelProvider": "gemini", "model": "gemini-2.0-flash", "modelApiKey": KEY},
        headers=auth,
    )

    assert response.status_code == 200, response.text
    value = response.json()["value"]
    assert value["ok"] is True
    assert value["status"] == "ok"
    assert value["model"] == "gemini-2.0-flash"
    assert provider.keys == [KEY], "the key under test is the one that was used"


async def test_a_rejected_key_is_a_200_saying_so(client: AsyncClient, provider: Provider) -> None:
    """Not a 4xx: the request to test succeeded, and its answer is that the key does not work."""
    provider.error = LLMAuthenticationError("API key not valid")
    auth = await headers(client)

    response = await client.post(
        "/agents/model-key/test",
        json={"modelProvider": "gemini", "model": "gemini-2.0-flash", "modelApiKey": "wrong"},
        headers=auth,
    )

    assert response.status_code == 200
    value = response.json()["value"]
    assert value["ok"] is False
    assert value["status"] == "invalid_key"
    assert "API key not valid" in value["detail"]


async def test_a_standalone_check_stores_nothing(client: AsyncClient, provider: Provider) -> None:
    auth = await headers(client)

    await client.post(
        "/agents/model-key/test",
        json={"modelProvider": "gemini", "model": "gemini-2.0-flash", "modelApiKey": KEY},
        headers=auth,
    )

    listed = (await client.get("/agents", headers=auth)).json()["value"]
    assert listed == [], "testing a key must not bring an agent into existence"


async def test_an_agent_check_uses_the_stored_key(client: AsyncClient, provider: Provider) -> None:
    auth = await headers(client)
    agent = await create(client, auth, name="Testable", **CONFIGURED, modelApiKey=KEY)

    response = await client.post(f"/agents/{agent['id']}/model-key/test", json={}, headers=auth)

    assert response.status_code == 200, response.text
    assert response.json()["value"]["ok"] is True
    assert provider.keys == [KEY]


async def test_an_agent_check_can_try_an_unsaved_key(
    client: AsyncClient, session: AsyncSession, provider: Provider
) -> None:
    """Testing before saving is the point — otherwise the only way to find out is to store it."""
    auth = await headers(client)
    agent = await create(client, auth, name="Trying", **CONFIGURED, modelApiKey=KEY)

    response = await client.post(
        f"/agents/{agent['id']}/model-key/test",
        json={"modelApiKey": "a-different-key", "model": "gemini-1.5-flash"},
        headers=auth,
    )

    assert response.status_code == 200
    assert response.json()["value"]["model"] == "gemini-1.5-flash"
    assert provider.keys == ["a-different-key"]
    assert await stored_key(session, agent["id"]) == KEY, "a check must not write anything"


async def test_checking_an_unconfigured_agent_says_what_is_missing(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await create(client, auth, name="Empty")

    response = await client.post(f"/agents/{agent['id']}/model-key/test", json={}, headers=auth)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "AGENT_NOT_CONFIGURED"


async def test_another_tenants_agent_cannot_be_probed(client: AsyncClient) -> None:
    """The same 404 an unknown id gets, so a key's existence cannot be probed from outside."""
    mine = await headers(client)
    agent = await create(client, mine, name="Mine", **CONFIGURED, modelApiKey=KEY)
    theirs = await headers(client)

    response = await client.post(f"/agents/{agent['id']}/model-key/test", json={}, headers=theirs)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"
