"""Cross-origin policy: the console's own origins, and a chat API any site may embed (§5.7)."""

from __future__ import annotations

from httpx import AsyncClient, Headers

from src import configs

SITE = "https://shop.example.com"


def exposed(headers: Headers) -> set[str]:
    raw = headers.get("access-control-expose-headers", "")
    return {header.strip().lower() for header in raw.split(",") if header.strip()}


async def test_a_console_response_exposes_the_headers_the_console_reads(
    client: AsyncClient,
) -> None:
    """Without this a cross-origin script sees none of them — rate limits, request ids, all."""
    console = configs.CORS_ALLOW_ORIGINS[0]

    response = await client.get("/health", headers={"Origin": console})

    assert response.headers["access-control-allow-origin"] == console
    assert response.headers["access-control-allow-credentials"] == "true"
    assert {
        "x-request-id",
        "x-conversation-id",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "retry-after",
    } <= exposed(response.headers)


async def test_a_console_route_does_not_answer_an_unknown_origin(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"Origin": SITE})

    assert "access-control-allow-origin" not in response.headers


async def test_the_console_policy_is_not_widened_by_the_chat_policy(client: AsyncClient) -> None:
    response = await client.options(
        "/agents", headers={"Origin": SITE, "Access-Control-Request-Method": "GET"}
    )

    assert response.status_code == 400


async def test_any_site_may_preflight_the_public_chat_api(client: AsyncClient) -> None:
    """A tenant's new website must not wait for an operator to edit an environment variable."""
    response = await client.options(
        "/v1/chat/messages/stream",
        headers={
            "Origin": SITE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    # The key travels in a header, never a cookie, so no credentials are ever allowed here.
    assert "access-control-allow-credentials" not in response.headers


async def test_the_public_chat_api_exposes_its_headers_to_any_site(client: AsyncClient) -> None:
    response = await client.get("/v1/chat/session?userId=visitor-1", headers={"Origin": SITE})

    assert response.status_code == 401, "no key was sent; the policy applies to the refusal too"
    assert response.headers["access-control-allow-origin"] == "*"
    assert {"x-request-id", "x-conversation-id", "retry-after"} <= exposed(response.headers)
