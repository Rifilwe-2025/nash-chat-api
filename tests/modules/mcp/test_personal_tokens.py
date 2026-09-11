"""Personal access tokens: issuing, listing, revoking, and who may see which.

As with API keys, the assertions that matter are about what is *not* returned and what is *not*
reachable: the secret never appears in a read, it can be copied again only through an explicit
request and only from an encrypted copy, and a token is visible only to the person who issued it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mcp.domain.services import PersonalAccessTokenService
from src.modules.mcp.internal.token_generator import generate_token, hash_token
from src.modules.tenants.domain.models import Tenant, User
from src.shared.database.pagination import PageRequest
from src.shared.exceptions import NotFoundException, UnauthorizedException
from tests.modules.mcp.helpers import account

# 32 bytes, base64 — the shape `SECURITY_ENCRYPTION_KEY` requires.
ENCRYPTION_KEY = "bmFzaC10ZXN0LWVuY3J5cHRpb24ta2V5LTMyLWJ5dGU="


async def issue(client: AsyncClient, auth: dict[str, str], **payload: Any) -> tuple[int, Any]:
    response = await client.post(
        "/mcp-tokens", json={"name": "Claude Code on my laptop", **payload}, headers=auth
    )
    return response.status_code, response.json()


# -- generation ------------------------------------------------------------------------


def test_a_token_is_prefixed_so_a_leak_is_greppable() -> None:
    generated = generate_token()

    assert generated.secret.startswith("nsp_")
    assert generated.prefix == generated.secret[:12]
    assert generated.token_hash == hash_token(generated.secret) != generated.secret


def test_two_tokens_are_never_the_same() -> None:
    assert generate_token().secret != generate_token().secret


# -- issuing ----------------------------------------------------------------------------


async def test_the_secret_is_never_in_a_read(client: AsyncClient) -> None:
    auth, _ = await account(client)

    status, body = await issue(client, auth)

    assert status == 201
    secret = body["value"]["token"]
    assert secret.startswith("nsp_")

    token_id = body["value"]["personalToken"]["id"]
    fetched = await client.get(f"/mcp-tokens/{token_id}", headers=auth)
    listed = await client.get("/mcp-tokens", headers=auth)
    assert secret not in fetched.text
    assert secret not in listed.text
    assert fetched.json()["value"]["prefix"] in secret


async def test_a_token_is_read_only_unless_write_is_asked_for(client: AsyncClient) -> None:
    auth, _ = await account(client)

    _, default = await issue(client, auth)
    _, writer = await issue(client, auth, scopes=["mcp:read", "mcp:write"])

    assert default["value"]["personalToken"]["scopes"] == ["mcp:read"]
    assert writer["value"]["personalToken"]["scopes"] == ["mcp:read", "mcp:write"]


async def test_an_unknown_scope_is_rejected(client: AsyncClient) -> None:
    auth, _ = await account(client)

    status, body = await issue(client, auth, scopes=["mcp:admin"])

    assert status == 422
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_an_expiry_in_the_past_is_rejected(client: AsyncClient) -> None:
    auth, _ = await account(client)
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    status, body = await issue(client, auth, expiresAt=past)

    assert status == 422
    assert body["error"]["code"] == "PERSONAL_TOKEN_EXPIRY_IN_PAST"


# -- management ---------------------------------------------------------------------------


async def test_revoking_marks_the_token_dead_but_keeps_the_record(client: AsyncClient) -> None:
    auth, _ = await account(client)
    _, body = await issue(client, auth)
    token_id = body["value"]["personalToken"]["id"]

    first = await client.post(f"/mcp-tokens/{token_id}/revoke", headers=auth)
    second = await client.post(f"/mcp-tokens/{token_id}/revoke", headers=auth)

    assert first.status_code == 200
    assert first.json()["value"]["active"] is False
    assert first.json()["value"]["revokedAt"] == second.json()["value"]["revokedAt"]
    listed = (await client.get("/mcp-tokens", headers=auth)).json()
    assert listed["meta"]["totalItems"] == 1


async def test_another_accounts_token_is_reported_as_missing(client: AsyncClient) -> None:
    mine, _ = await account(client)
    theirs, _ = await account(client)
    _, body = await issue(client, mine)
    token_id = body["value"]["personalToken"]["id"]

    for response in (
        await client.get(f"/mcp-tokens/{token_id}", headers=theirs),
        await client.get(f"/mcp-tokens/{token_id}/secret", headers=theirs),
        await client.post(f"/mcp-tokens/{token_id}/revoke", headers=theirs),
        await client.delete(f"/mcp-tokens/{token_id}", headers=theirs),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PERSONAL_TOKEN_NOT_FOUND"

    assert (await client.get("/mcp-tokens", headers=theirs)).json()["meta"]["totalItems"] == 0


async def test_tokens_are_personal_within_an_organisation(
    session: AsyncSession,
    make_tenant: Callable[..., Coroutine[Any, Any, Tenant]],
    make_user: Callable[..., Coroutine[Any, Any, User]],
) -> None:
    """Two people in one tenant: neither lists nor revokes the other's credential."""
    tenant = await make_tenant()
    owner = await make_user(tenant)
    colleague = await make_user(tenant)
    token, _ = await PersonalAccessTokenService(session, tenant.id, owner.id).issue("Laptop")

    theirs = PersonalAccessTokenService(session, tenant.id, colleague.id)

    assert (await theirs.list_tokens(PageRequest())).total == 0
    with pytest.raises(NotFoundException):
        await theirs.copy_secret(token.id)
    with pytest.raises(NotFoundException):
        await theirs.revoke(token.id)
    with pytest.raises(NotFoundException):
        await theirs.delete(token.id)


async def test_token_management_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/mcp-tokens")).status_code == 401
    assert (await client.post("/mcp-tokens", json={"name": "x"})).status_code == 401
    assert (await client.get("/mcp-tokens/connection")).status_code == 401
    assert (await client.get(f"/mcp-tokens/{uuid.uuid4()}/secret")).status_code == 401
    assert (await client.post(f"/mcp-tokens/{uuid.uuid4()}/revoke")).status_code == 401
    assert (await client.delete(f"/mcp-tokens/{uuid.uuid4()}")).status_code == 401


async def test_connection_details_name_the_endpoint_and_scopes(client: AsyncClient) -> None:
    auth, _ = await account(client)

    response = await client.get("/mcp-tokens/connection", headers=auth)

    value = response.json()["value"]
    assert response.status_code == 200
    assert value["enabled"] is True
    assert value["serverUrl"] == "http://test/mcp"
    assert value["transport"] == "streamable-http"
    assert {scope["scope"] for scope in value["scopes"]} == {"mcp:read", "mcp:write"}
    assert value["rateLimitPerMinute"] > 0


async def test_the_connection_url_follows_the_public_base_url(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(PUBLIC_BASE_URL="https://api.example.com")
    auth, _ = await account(client)

    value = (await client.get("/mcp-tokens/connection", headers=auth)).json()["value"]

    assert value["serverUrl"] == "https://api.example.com/mcp"


# -- copying ------------------------------------------------------------------------------


async def stored_copy(session: AsyncSession, token_id: str) -> str | None:
    """Raw SQL on purpose: the column must be read as a database dump would see it."""
    value: str | None = await session.scalar(
        text("SELECT encrypted_secret FROM personal_access_token WHERE id = :id"), {"id": token_id}
    )
    return value


async def test_a_token_can_be_copied_again_with_an_encryption_key(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(SECURITY_ENCRYPTION_KEY=ENCRYPTION_KEY)
    auth, _ = await account(client)
    _, body = await issue(client, auth)
    token_id = body["value"]["personalToken"]["id"]

    response = await client.get(f"/mcp-tokens/{token_id}/secret", headers=auth)

    assert body["value"]["personalToken"]["copyable"] is True
    assert response.status_code == 200
    assert response.json()["value"]["token"] == body["value"]["token"]
    assert response.headers["cache-control"] == "no-store"
    listed = (await client.get("/mcp-tokens", headers=auth)).json()["value"]
    assert listed[0]["copyable"] is True
    assert body["value"]["token"] not in str(listed)


async def test_the_copy_is_stored_encrypted(
    client: AsyncClient, session: AsyncSession, config_override: Callable[..., None]
) -> None:
    config_override(SECURITY_ENCRYPTION_KEY=ENCRYPTION_KEY)
    auth, _ = await account(client)
    _, body = await issue(client, auth)

    raw = await stored_copy(session, body["value"]["personalToken"]["id"])

    assert raw is not None
    assert raw.startswith("v1:"), "stored as the versioned envelope, not in clear"
    assert body["value"]["token"] not in raw


async def test_without_an_encryption_key_no_copy_is_kept(
    client: AsyncClient, session: AsyncSession, config_override: Callable[..., None]
) -> None:
    """Storing the secret in clear would undo the hash, so without a key nothing is stored."""
    config_override(SECURITY_ENCRYPTION_KEY="")
    auth, _ = await account(client)
    _, body = await issue(client, auth)
    token_id = body["value"]["personalToken"]["id"]

    response = await client.get(f"/mcp-tokens/{token_id}/secret", headers=auth)

    assert body["value"]["personalToken"]["copyable"] is False
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PERSONAL_TOKEN_NOT_COPYABLE"
    assert await stored_copy(session, token_id) is None


async def test_revoking_discards_the_copy(
    client: AsyncClient, session: AsyncSession, config_override: Callable[..., None]
) -> None:
    config_override(SECURITY_ENCRYPTION_KEY=ENCRYPTION_KEY)
    auth, _ = await account(client)
    _, body = await issue(client, auth)
    token_id = body["value"]["personalToken"]["id"]

    revoked = await client.post(f"/mcp-tokens/{token_id}/revoke", headers=auth)
    response = await client.get(f"/mcp-tokens/{token_id}/secret", headers=auth)

    assert revoked.json()["value"]["copyable"] is False
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PERSONAL_TOKEN_NOT_COPYABLE"
    assert await stored_copy(session, token_id) is None


# -- deleting -----------------------------------------------------------------------------


async def test_deleting_removes_the_token_and_it_is_refused(
    client: AsyncClient, session: AsyncSession
) -> None:
    auth, _ = await account(client)
    _, body = await issue(client, auth)
    token_id = body["value"]["personalToken"]["id"]

    response = await client.delete(f"/mcp-tokens/{token_id}", headers=auth)

    assert response.status_code == 200
    assert (await client.get(f"/mcp-tokens/{token_id}", headers=auth)).status_code == 404
    assert (await client.get("/mcp-tokens", headers=auth)).json()["meta"]["totalItems"] == 0
    with pytest.raises(UnauthorizedException):
        await PersonalAccessTokenService.authenticate(session, body["value"]["token"])


async def test_a_revoked_token_can_still_be_deleted(client: AsyncClient) -> None:
    auth, _ = await account(client)
    _, body = await issue(client, auth)
    token_id = body["value"]["personalToken"]["id"]
    await client.post(f"/mcp-tokens/{token_id}/revoke", headers=auth)

    response = await client.delete(f"/mcp-tokens/{token_id}", headers=auth)

    assert response.status_code == 200
    assert (await client.get("/mcp-tokens", headers=auth)).json()["meta"]["totalItems"] == 0
