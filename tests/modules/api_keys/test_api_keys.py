"""API key issue, scope, revocation and rate limiting (spec §5.6).

The security-relevant assertions are the ones about what is *not* returned and what is *not*
distinguishable: the secret appears exactly once, and every authentication failure looks the same
from outside.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from src.core.rate_limit import InMemoryBackend, RateLimiter
from src.modules.api_keys.internal.key_generator import generate_key, hash_key
from tests.modules.auth.test_auth_flow import auth_header, signup

PUBLISHABLE: dict[str, Any] = {
    "persona": "You are the sales assistant for Nash Paints.",
    "modelProvider": "gemini",
    "modelSettings": {"model": "gemini-2.0-flash", "temperature": 0.5, "maxTokens": 512},
}


async def headers(client: AsyncClient) -> dict[str, str]:
    return auth_header((await signup(client))["tokens"])


async def published_agent(client: AsyncClient, auth: dict[str, str]) -> dict[str, Any]:
    created = await client.post(
        "/agents", json={"name": f"Agent {uuid.uuid4().hex[:6]}", **PUBLISHABLE}, headers=auth
    )
    agent: dict[str, Any] = created.json()["value"]
    await client.post(f"/agents/{agent['id']}/publish", headers=auth)
    return agent


async def issue(
    client: AsyncClient, auth: dict[str, str], agent_id: str, **payload: Any
) -> tuple[int, dict[str, Any]]:
    response = await client.post(
        f"/api-keys?agentId={agent_id}",
        json={"name": "Website widget", **payload},
        headers=auth,
    )
    return response.status_code, response.json()


# -- generation ------------------------------------------------------------------------


def test_a_key_is_prefixed_so_a_leak_is_greppable() -> None:
    """Secret scanners find credentials by recognisable prefixes; a bare random string is
    invisible to every one of them."""
    generated = generate_key()

    assert generated.secret.startswith("nsk_")
    assert generated.prefix == generated.secret[:12]
    assert len(generated.secret) > 30


def test_the_stored_hash_is_not_the_secret() -> None:
    generated = generate_key()

    assert generated.key_hash != generated.secret
    assert generated.key_hash == hash_key(generated.secret)
    assert len(generated.key_hash) == 64


def test_two_keys_are_never_the_same() -> None:
    assert generate_key().secret != generate_key().secret


# -- issuing ----------------------------------------------------------------------------


async def test_the_secret_is_returned_exactly_once(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)

    status, body = await issue(client, auth, agent["id"])

    assert status == 201
    secret = body["value"]["key"]
    assert secret.startswith("nsk_")

    key_id = body["value"]["apiKey"]["id"]
    fetched = await client.get(f"/api-keys/{key_id}", headers=auth)
    assert secret not in fetched.text, "the secret must never be readable again"
    assert fetched.json()["value"]["prefix"] in secret


async def test_a_key_defaults_to_both_scopes_and_the_plan_rate(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)

    _, body = await issue(client, auth, agent["id"])

    api_key = body["value"]["apiKey"]
    assert set(api_key["scopes"]) == {"chat:write", "chat:read"}
    assert api_key["rateLimitPerMinute"] > 0
    assert api_key["active"] is True


async def test_scopes_can_be_narrowed_at_issue(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)

    _, body = await issue(client, auth, agent["id"], scopes=["chat:write"])

    assert body["value"]["apiKey"]["scopes"] == ["chat:write"]


async def test_an_unknown_scope_is_rejected(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)

    status, body = await issue(client, auth, agent["id"], scopes=["chat:admin"])

    assert status == 422


async def test_an_expiry_in_the_past_is_rejected(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    status, body = await issue(client, auth, agent["id"], expiresAt=past)

    assert status == 422
    assert body["error"]["code"] == "API_KEY_EXPIRY_IN_PAST"


async def test_a_rate_limit_beyond_the_maximum_is_rejected(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(RATE_LIMIT_MAX_PER_MINUTE=100)
    auth = await headers(client)
    agent = await published_agent(client, auth)

    status, body = await issue(client, auth, agent["id"], rateLimitPerMinute=101)

    assert status == 422
    assert body["error"]["code"] == "INVALID_RATE_LIMIT"


# -- management ---------------------------------------------------------------------------


async def test_keys_are_listed_without_their_secrets(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)
    _, first = await issue(client, auth, agent["id"])

    body = (await client.get("/api-keys", headers=auth)).json()

    assert body["meta"]["totalItems"] == 1
    assert first["value"]["key"] not in (await client.get("/api-keys", headers=auth)).text
    assert body["value"][0]["prefix"]


async def test_scopes_can_be_narrowed_without_reissuing(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)
    _, issued = await issue(client, auth, agent["id"])
    key_id = issued["value"]["apiKey"]["id"]

    response = await client.patch(
        f"/api-keys/{key_id}", json={"scopes": ["chat:read"]}, headers=auth
    )

    assert response.status_code == 200
    assert response.json()["value"]["scopes"] == ["chat:read"]


async def test_revoking_marks_the_key_dead_but_keeps_the_record(client: AsyncClient) -> None:
    """Kept rather than deleted so past use stays attributable and the tenant can see it."""
    auth = await headers(client)
    agent = await published_agent(client, auth)
    _, issued = await issue(client, auth, agent["id"])
    key_id = issued["value"]["apiKey"]["id"]

    response = await client.post(f"/api-keys/{key_id}/revoke", headers=auth)

    assert response.status_code == 200
    assert response.json()["value"]["active"] is False
    assert response.json()["value"]["revokedAt"]
    assert (await client.get(f"/api-keys/{key_id}", headers=auth)).status_code == 200


async def test_revoking_twice_is_a_no_op(client: AsyncClient) -> None:
    auth = await headers(client)
    agent = await published_agent(client, auth)
    _, issued = await issue(client, auth, agent["id"])
    key_id = issued["value"]["apiKey"]["id"]

    first = await client.post(f"/api-keys/{key_id}/revoke", headers=auth)
    second = await client.post(f"/api-keys/{key_id}/revoke", headers=auth)

    assert second.status_code == 200
    assert first.json()["value"]["revokedAt"] == second.json()["value"]["revokedAt"]


# -- isolation -----------------------------------------------------------------------------


async def test_another_tenants_key_is_reported_as_missing(client: AsyncClient) -> None:
    first = await headers(client)
    second = await headers(client)
    agent = await published_agent(client, first)
    _, issued = await issue(client, first, agent["id"])
    key_id = issued["value"]["apiKey"]["id"]

    for method, suffix, payload in (
        ("get", "", None),
        ("patch", "", {"name": "hijacked"}),
        ("post", "/revoke", None),
    ):
        kwargs: dict[str, Any] = {"headers": second}
        if payload is not None:
            kwargs["json"] = payload
        response = await getattr(client, method)(f"/api-keys/{key_id}{suffix}", **kwargs)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "API_KEY_NOT_FOUND"


async def test_a_key_cannot_be_issued_for_another_tenants_agent(client: AsyncClient) -> None:
    first = await headers(client)
    second = await headers(client)
    agent = await published_agent(client, first)

    status, body = await issue(client, second, agent["id"])

    assert status == 404
    assert body["error"]["code"] == "AGENT_NOT_FOUND"


async def test_key_management_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/api-keys")).status_code == 401
    assert (
        await client.post(f"/api-keys?agentId={uuid.uuid4()}", json={"name": "x"})
    ).status_code == 401


# -- the rate limiter itself -----------------------------------------------------------------


async def test_the_limiter_allows_up_to_the_limit_then_refuses() -> None:
    limiter = RateLimiter(InMemoryBackend())

    verdicts = [await limiter.check("key", limit=3) for _ in range(4)]

    assert [verdict.allowed for verdict in verdicts] == [True, True, True, False]
    assert [verdict.remaining for verdict in verdicts] == [2, 1, 0, 0]


async def test_the_limiter_counts_each_key_separately() -> None:
    limiter = RateLimiter(InMemoryBackend())

    await limiter.check("first", limit=1)
    verdict = await limiter.check("second", limit=1)

    assert verdict.allowed is True


async def test_a_limit_of_zero_means_unlimited() -> None:
    limiter = RateLimiter(InMemoryBackend())

    for _ in range(50):
        assert (await limiter.check("key", limit=0)).allowed


async def test_a_broken_backend_fails_open() -> None:
    """A limiter outage must not reject every customer's traffic."""

    class Broken:
        async def increment(self, key: str, window: int) -> int:
            raise RuntimeError("redis is down")

    verdict = await RateLimiter(Broken()).check("key", limit=1)

    assert verdict.allowed is True


async def test_the_refusal_carries_retry_after() -> None:
    limiter = RateLimiter(InMemoryBackend())
    await limiter.check("key", limit=1)

    verdict = await limiter.check("key", limit=1)

    assert verdict.allowed is False
    headers = verdict.headers()
    assert headers["Retry-After"] == str(verdict.retry_after)
    assert headers["X-RateLimit-Remaining"] == "0"


@pytest.mark.parametrize("limit", [1, 5, 100])
async def test_every_verdict_reports_the_limit(limit: int) -> None:
    verdict = await RateLimiter(InMemoryBackend()).check("key", limit=limit)

    assert verdict.headers()["X-RateLimit-Limit"] == str(limit)
