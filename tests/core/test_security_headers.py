"""Response hardening headers, and the throttle on the credential endpoints (Phase 13, §5.7)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from httpx import AsyncClient

from tests.modules.auth.test_auth_flow import PASSWORD, unique_email


async def test_every_response_carries_the_hardening_headers(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-site"


async def test_an_error_response_carries_them_too(client: AsyncClient) -> None:
    """The headers are applied outside the error handlers, so a 401 is protected like a 200."""
    response = await client.get("/analytics/usage")

    assert response.status_code == 401
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_hsts_is_not_sent_over_plain_http(client: AsyncClient) -> None:
    """Sent on an http response it is ignored by browsers and misleading to anyone reading it."""
    response = await client.get("/health")

    assert "strict-transport-security" not in response.headers


async def test_hsts_is_sent_over_https(client: AsyncClient) -> None:
    response = await client.get("https://test/health")

    assert response.headers["strict-transport-security"].startswith("max-age=")
    assert "includeSubDomains" in response.headers["strict-transport-security"]


async def test_the_docs_carry_a_content_security_policy(client: AsyncClient) -> None:
    """`/docs` is the only HTML this service serves, so it is the only place a CSP belongs."""
    response = await client.get("/docs")

    policy = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in policy
    assert "script-src 'self' https://cdn.jsdelivr.net" in policy


async def test_a_json_route_has_no_content_security_policy(client: AsyncClient) -> None:
    """A document policy on a JSON response documents nothing and eventually breaks something."""
    assert "content-security-policy" not in (await client.get("/health")).headers


# -- the credential throttle ---------------------------------------------------------


@pytest.fixture
def tight_limit(config_override: Callable[..., None]) -> None:
    config_override(RATE_LIMIT_AUTH_PER_MINUTE=3)


async def test_repeated_sign_in_attempts_are_throttled(
    client: AsyncClient, tight_limit: None
) -> None:
    """Password guessing stops before the hash is computed, which is the expensive part."""
    payload = {"email": unique_email(), "password": PASSWORD}

    statuses = [(await client.post("/auth/login", json=payload)).status_code for _ in range(5)]

    assert statuses[:3] == [401, 401, 401]
    assert statuses[-1] == 429


async def test_a_throttled_response_says_when_to_come_back(
    client: AsyncClient, tight_limit: None
) -> None:
    payload = {"email": unique_email(), "password": PASSWORD}
    for _ in range(4):
        response = await client.post("/auth/login", json=payload)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert int(response.headers["retry-after"]) >= 1
    assert response.headers["x-ratelimit-limit"] == "3"


async def test_a_successful_response_reports_the_remaining_allowance(
    client: AsyncClient, tight_limit: None
) -> None:
    """A client should learn its allowance from a success, not only from being refused."""
    response = await client.post(
        "/auth/signup",
        json={"email": unique_email(), "password": PASSWORD, "tenantName": "Acme"},
    )

    assert response.status_code == 201
    assert response.headers["x-ratelimit-limit"] == "3"
    assert int(response.headers["x-ratelimit-remaining"]) >= 0
