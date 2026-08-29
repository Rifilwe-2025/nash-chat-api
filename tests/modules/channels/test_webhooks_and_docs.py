"""Webhook signing and generated integration docs (spec §5.6).

The signing tests matter most: a webhook URL is not a secret, so the signature is the only thing
separating a real delivery from anyone who guessed the endpoint.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from src.modules.channels.internal import integration_docs, webhooks
from tests.modules.auth.test_auth_flow import auth_header, signup

PUBLISHABLE: dict[str, Any] = {
    "persona": "You are the sales assistant for Nash Paints.",
    "modelProvider": "gemini",
    "modelSettings": {"model": "gemini-2.0-flash", "temperature": 0.5, "maxTokens": 512},
}


async def owner(client: AsyncClient) -> dict[str, str]:
    return auth_header((await signup(client))["tokens"])


async def published_agent(client: AsyncClient, auth: dict[str, str]) -> dict[str, Any]:
    created = await client.post(
        "/agents", json={"name": f"Agent {uuid.uuid4().hex[:6]}", **PUBLISHABLE}, headers=auth
    )
    agent: dict[str, Any] = created.json()["value"]
    await client.post(f"/agents/{agent['id']}/publish", headers=auth)
    return agent


# -- signing ---------------------------------------------------------------------------


def test_a_delivery_verifies_against_its_own_secret() -> None:
    secret = webhooks.generate_secret()
    payload = webhooks.build_payload("conversation.started", {"conversationId": "abc"})
    header = webhooks.signature_for(secret, payload, int(time.time()))

    assert webhooks.verify_signature(secret, payload, header) is True


def test_a_delivery_does_not_verify_against_another_secret() -> None:
    payload = webhooks.build_payload("conversation.started", {})
    header = webhooks.signature_for(webhooks.generate_secret(), payload, int(time.time()))

    assert webhooks.verify_signature(webhooks.generate_secret(), payload, header) is False


def test_a_tampered_body_does_not_verify() -> None:
    secret = webhooks.generate_secret()
    payload = webhooks.build_payload("conversation.started", {"conversationId": "abc"})
    header = webhooks.signature_for(secret, payload, int(time.time()))

    assert webhooks.verify_signature(secret, payload + " ", header) is False


def test_an_old_signature_is_refused_even_though_it_verifies() -> None:
    """Without the timestamp check a captured delivery is replayable forever."""
    secret = webhooks.generate_secret()
    payload = webhooks.build_payload("conversation.started", {})
    stale = webhooks.signature_for(secret, payload, int(time.time()) - 3600)

    assert webhooks.verify_signature(secret, payload, stale) is False
    assert webhooks.verify_signature(secret, payload, stale, tolerance_seconds=7200) is True


@pytest.mark.parametrize("header", ["", "nonsense", "t=abc,v1=xyz", "v1=onlysignature"])
def test_a_malformed_signature_header_is_refused_rather_than_crashing(header: str) -> None:
    assert webhooks.verify_signature("secret", "{}", header) is False


def test_two_secrets_are_never_the_same() -> None:
    assert webhooks.generate_secret() != webhooks.generate_secret()
    assert webhooks.generate_secret().startswith("whsec_")


async def test_a_delivery_failure_is_a_value_not_an_exception() -> None:
    """The caller is a fire-and-forget task on the conversation path — there is nobody to catch."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        delivered, error = await webhooks.deliver("https://example.com/x", "s", "{}", client=http)

    assert delivered is False
    assert error == "ConnectError"


async def test_a_delivery_carries_the_signature_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200)

    secret = webhooks.generate_secret()
    payload = webhooks.build_payload("conversation.started", {})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        delivered, _ = await webhooks.deliver("https://example.com/x", secret, payload, client=http)

    assert delivered is True
    assert webhooks.verify_signature(secret, payload, seen["x-nash-signature"]) is True


async def test_an_error_status_counts_as_a_failed_delivery() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        delivered, error = await webhooks.deliver("https://example.com/x", "s", "{}", client=http)

    assert delivered is False
    assert error == "HTTP 500"


# -- endpoint management ------------------------------------------------------------------


async def test_an_endpoint_is_created_with_a_signing_secret(client: AsyncClient) -> None:
    auth = await owner(client)

    response = await client.post(
        "/webhooks",
        json={"url": "https://example.com/hooks", "events": ["conversation.escalated"]},
        headers=auth,
    )

    assert response.status_code == 201
    assert response.json()["value"]["secret"].startswith("whsec_")
    assert response.json()["value"]["events"] == ["conversation.escalated"]


async def test_an_unknown_event_is_rejected(client: AsyncClient) -> None:
    auth = await owner(client)

    response = await client.post(
        "/webhooks",
        json={"url": "https://example.com/hooks", "events": ["conversation.exploded"]},
        headers=auth,
    )

    assert response.status_code == 422


async def test_an_endpoint_can_be_disabled_without_losing_its_secret(
    client: AsyncClient,
) -> None:
    auth = await owner(client)
    created = await client.post(
        "/webhooks",
        json={"url": "https://example.com/hooks", "events": ["conversation.started"]},
        headers=auth,
    )
    endpoint = created.json()["value"]

    response = await client.patch(
        f"/webhooks/{endpoint['id']}", json={"status": "disabled"}, headers=auth
    )

    assert response.json()["value"]["status"] == "disabled"
    assert response.json()["value"]["secret"] == endpoint["secret"]


async def test_an_endpoint_can_be_deleted(client: AsyncClient) -> None:
    auth = await owner(client)
    created = await client.post(
        "/webhooks",
        json={"url": "https://example.com/hooks", "events": ["conversation.started"]},
        headers=auth,
    )

    deleted = await client.delete(f"/webhooks/{created.json()['value']['id']}", headers=auth)

    assert deleted.status_code == 200
    assert (await client.get("/webhooks", headers=auth)).json()["meta"]["totalItems"] == 0


async def test_another_tenants_endpoint_is_reported_as_missing(client: AsyncClient) -> None:
    first = await owner(client)
    second = await owner(client)
    created = await client.post(
        "/webhooks",
        json={"url": "https://example.com/hooks", "events": ["conversation.started"]},
        headers=first,
    )
    endpoint_id = created.json()["value"]["id"]

    response = await client.patch(
        f"/webhooks/{endpoint_id}", json={"url": "https://evil.example"}, headers=second
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WEBHOOK_NOT_FOUND"


async def test_endpoints_from_another_tenant_are_invisible(client: AsyncClient) -> None:
    first = await owner(client)
    second = await owner(client)
    await client.post(
        "/webhooks",
        json={"url": "https://example.com/hooks", "events": ["conversation.started"]},
        headers=first,
    )

    assert (await client.get("/webhooks", headers=second)).json()["meta"]["totalItems"] == 0


async def test_webhook_routes_require_authentication(client: AsyncClient) -> None:
    assert (await client.get("/webhooks")).status_code == 401
    assert (await client.post("/webhooks", json={"url": "x", "events": []})).status_code == 401


# -- generated integration docs -------------------------------------------------------------


def test_the_endpoint_list_comes_from_the_schema_not_a_hand_written_list() -> None:
    """A hand-written list drifts the first time a route changes; a generated one cannot."""
    schema = {
        "paths": {
            "/v1/chat/messages": {
                "post": {"summary": "Send a message", "description": "Body."},
            },
            "/agents": {"get": {"summary": "Not public", "description": "Excluded."}},
        }
    }

    markdown = integration_docs.build(
        agent_name="Sales Assistant",
        agent_id="agent-1",
        base_url="https://api.example.com",
        key_prefix="nsk_live_abc",
        scopes=["chat:write"],
        rate_limit=60,
        signature_header="X-Nash-Signature",
        schema=schema,
    )

    assert "POST /v1/chat/messages" in markdown
    assert "Send a message" in markdown
    assert "Not public" not in markdown, "only the public chat surface is documented"


async def test_the_guide_is_generated_for_a_real_agent(client: AsyncClient) -> None:
    auth = await owner(client)
    agent = await published_agent(client, auth)

    response = await client.get(f"/agents/{agent['id']}/integration-docs", headers=auth)

    assert response.status_code == 200
    markdown = response.json()["value"]["markdown"]
    assert agent["name"] in markdown
    assert agent["id"] in markdown
    for section in ("## Quickstart", "## Sessions", "## Escalation", "## Rate limits", "## Errors"):
        assert section in markdown
    assert "POST /v1/chat/messages" in markdown


async def test_the_guide_uses_a_real_keys_prefix_and_limit(client: AsyncClient) -> None:
    auth = await owner(client)
    agent = await published_agent(client, auth)
    issued = await client.post(
        f"/api-keys?agentId={agent['id']}",
        json={"name": "Widget", "scopes": ["chat:write"], "rateLimitPerMinute": 42},
        headers=auth,
    )
    api_key = issued.json()["value"]["apiKey"]

    response = await client.get(
        f"/agents/{agent['id']}/integration-docs?apiKeyId={api_key['id']}", headers=auth
    )

    markdown = response.json()["value"]["markdown"]
    assert api_key["prefix"] in markdown
    assert "42 requests per minute" in markdown
    assert issued.json()["value"]["key"] not in markdown, "the secret never appears in docs"


async def test_the_guide_will_not_use_another_agents_key(client: AsyncClient) -> None:
    auth = await owner(client)
    first = await published_agent(client, auth)
    second = await published_agent(client, auth)
    issued = await client.post(
        f"/api-keys?agentId={second['id']}", json={"name": "Widget"}, headers=auth
    )

    response = await client.get(
        f"/agents/{first['id']}/integration-docs"
        f"?apiKeyId={issued.json()['value']['apiKey']['id']}",
        headers=auth,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "API_KEY_NOT_FOUND"


async def test_docs_for_another_tenants_agent_are_reported_as_missing(
    client: AsyncClient,
) -> None:
    first = await owner(client)
    second = await owner(client)
    agent = await published_agent(client, first)

    response = await client.get(f"/agents/{agent['id']}/integration-docs", headers=second)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"
