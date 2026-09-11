"""Speaking MCP to the application under test.

The endpoint is exercised the way a coding agent reaches it — JSON-RPC over HTTP, with a bearer
token — rather than by calling tool functions directly, so authentication, rate limiting, the
per-call transaction and error translation are all on the path every assertion goes through.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient, Response

from tests.modules.auth.test_auth_flow import auth_header, signup

# A handshake-era revision, which the stateless transport serves without an initialize round trip.
PROTOCOL_VERSION = "2025-06-18"

# What the SDK puts in front of every failed tool call's message, ahead of the platform's own code.
SDK_ERROR_PREFIX = "Error executing tool "

PUBLISHABLE: dict[str, Any] = {
    "persona": "You are the sales assistant for Nash Paints.",
    "modelProvider": "gemini",
    "modelSettings": {"model": "gemini-2.0-flash", "temperature": 0.5, "maxTokens": 512},
}


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[None]:
    """Run the MCP session manager for the length of a block.

    A real process starts it in the lifespan, which httpx's ASGI transport never runs. It is entered
    inside the test body rather than in a fixture because the manager's task group has to be exited
    by the same task that entered it, and pytest runs a fixture's setup and teardown in different
    tasks.
    """
    async with app.state.mcp_server.session_manager.run():
        yield


async def account(client: AsyncClient) -> tuple[dict[str, str], dict[str, Any]]:
    """A fresh account and tenant: the auth header, and the sign-up payload."""
    value = await signup(client)
    return auth_header(value["tokens"]), value


async def issue_token(
    client: AsyncClient, auth: dict[str, str], scopes: tuple[str, ...] = ("mcp:read",)
) -> str:
    response = await client.post(
        "/mcp-tokens", json={"name": "Coding agent", "scopes": list(scopes)}, headers=auth
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["value"]["token"]
    return token


async def rpc(
    client: AsyncClient, token: str | None, method: str, params: dict[str, Any] | None = None
) -> Response:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers=headers,
    )


async def call_tool(
    client: AsyncClient, token: str, name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The tool's result. A tool that failed is still a 200 with ``isError`` — check for it."""
    response = await rpc(client, token, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["result"]
    return result


async def value_of(
    client: AsyncClient, token: str, name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The structured result of a tool that is expected to succeed."""
    result = await call_tool(client, token, name, arguments)
    assert not result.get("isError"), result
    value: dict[str, Any] = result["structuredContent"]
    return value


def error_text(result: dict[str, Any]) -> str:
    assert result.get("isError"), result
    text: str = result["content"][0]["text"]
    return text


def error_code(result: dict[str, Any]) -> str:
    """The platform's error code from a failed call: ``Error executing tool x: CODE: detail``."""
    text = error_text(result)
    assert text.startswith(SDK_ERROR_PREFIX), text
    _, _, message = text.partition(": ")
    code, _, _ = message.partition(":")
    return code
