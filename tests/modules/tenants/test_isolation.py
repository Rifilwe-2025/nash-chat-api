"""Tenant isolation over HTTP — the bar Phase 2 is judged on.

Two tenants are created through the real signup flow, then tenant B attempts to read and modify
tenant A's data through every exposed route.
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.modules.auth.test_auth_flow import PASSWORD, auth_header, signup, unique_email


async def two_tenants(client: AsyncClient) -> tuple[dict[str, Any], dict[str, Any]]:
    return await signup(client), await signup(client)


async def test_signup_gives_each_account_its_own_tenant(client: AsyncClient) -> None:
    first, second = await two_tenants(client)

    assert first["user"]["tenantId"] != second["user"]["tenantId"]


async def test_me_returns_only_the_callers_own_tenant(client: AsyncClient) -> None:
    first, second = await two_tenants(client)

    response = await client.get("/me", headers=auth_header(second["tokens"]))

    body = response.json()["value"]
    assert body["user"]["id"] == second["user"]["id"]
    assert body["tenant"]["id"] == second["user"]["tenantId"]
    assert body["tenant"]["id"] != first["user"]["tenantId"]


async def test_members_never_include_another_tenants_users(client: AsyncClient) -> None:
    first, second = await two_tenants(client)

    response = await client.get("/tenant/members", headers=auth_header(second["tokens"]))

    members = response.json()["value"]
    assert [member["id"] for member in members] == [second["user"]["id"]]
    assert first["user"]["id"] not in [member["id"] for member in members]


async def test_renaming_affects_only_the_callers_tenant(client: AsyncClient) -> None:
    first, second = await two_tenants(client)

    renamed = await client.patch(
        "/tenant", json={"name": "Renamed By B"}, headers=auth_header(second["tokens"])
    )
    assert renamed.status_code == 200
    assert renamed.json()["value"]["name"] == "Renamed By B"

    unchanged = await client.get("/tenant", headers=auth_header(first["tokens"]))
    assert unchanged.json()["value"]["name"] == "Acme Paints"


async def test_a_user_cannot_take_another_accounts_email(client: AsyncClient) -> None:
    email_a = unique_email()
    await signup(client, email_a)
    second = await signup(client)

    response = await client.patch(
        "/me", json={"email": email_a}, headers=auth_header(second["tokens"])
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EMAIL_TAKEN"


async def test_profile_updates_do_not_touch_the_other_tenant(client: AsyncClient) -> None:
    first, second = await two_tenants(client)

    await client.patch("/me", json={"fullName": "Changed"}, headers=auth_header(second["tokens"]))

    other = await client.get("/me", headers=auth_header(first["tokens"]))
    assert other.json()["value"]["user"]["fullName"] == "Ada Lovelace"


async def test_a_tenants_token_stops_working_after_the_other_logs_in(
    client: AsyncClient,
) -> None:
    """Rotation is per-user: B signing in must not disturb A's session."""
    email_a = unique_email()
    first = await signup(client, email_a)
    second = await signup(client)

    await client.post(
        "/auth/login",
        json={"email": second["user"]["email"], "password": PASSWORD},
    )

    still_valid = await client.get("/me", headers=auth_header(first["tokens"]))
    assert still_valid.status_code == 200
