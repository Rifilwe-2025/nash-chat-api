"""Platform administration and account status (plan Phase 15).

Three things carry the weight here, and they are the three that would be expensive to get wrong:

* **the flag is the whole boundary** — a signed-in tenant user is refused everywhere under `/admin`,
  and there is no route that grants the flag;
* **`X-Tenant-Id` widens scope for admins and for nobody else** — a non-admin sending it stays in
  their own tenant, which is the isolation invariant (§5.7) meeting the one feature designed to
  cross it;
* **disabling reaches every door** — the sign-in path, an existing token, and an API key.
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.modules.auth.test_auth_flow import PASSWORD, auth_header, signup


async def account(client: AsyncClient) -> tuple[dict[str, str], uuid.UUID, str]:
    """Sign up and keep the token, the tenant and the email."""
    value = await signup(client)
    return (
        auth_header(value["tokens"]),
        uuid.UUID(value["user"]["tenantId"]),
        value["user"]["email"],
    )


async def make_admin(session: AsyncSession, email: str) -> None:
    """Grant the flag the way the script does — by writing it, never through the API."""
    await session.execute(
        text('UPDATE "user" SET is_platform_admin = true WHERE lower(email) = lower(:email)'),
        {"email": email},
    )
    await session.flush()


async def admin_account(client: AsyncClient, session: AsyncSession) -> dict[str, str]:
    auth, _, email = await account(client)
    await make_admin(session, email)
    return auth


# -- the boundary --------------------------------------------------------------------


async def test_a_tenant_user_is_refused_every_admin_route(client: AsyncClient) -> None:
    auth, tenant_id, _ = await account(client)

    routes = [
        await client.get("/admin/tenants", headers=auth),
        await client.get(f"/admin/tenants/{tenant_id}", headers=auth),
        await client.get("/admin/overview", headers=auth),
        await client.put(
            f"/admin/tenants/{tenant_id}/status", json={"enabled": False}, headers=auth
        ),
    ]

    assert [response.status_code for response in routes] == [403, 403, 403, 403]
    assert routes[0].json()["error"]["code"] == "PLATFORM_ADMIN_REQUIRED"


async def test_admin_routes_need_a_token_at_all(client: AsyncClient) -> None:
    assert (await client.get("/admin/tenants")).status_code == 401


async def test_no_endpoint_grants_the_flag(client: AsyncClient) -> None:
    """The flag is set out of band by a script. An API that grants it is an API that escalates."""
    schema = (await client.get("/openapi.json")).json()

    granting = [
        f"{method} {path}"
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if "isPlatformAdmin"
        in str(operation.get("requestBody", {})) + str(operation.get("parameters", []))
    ]

    assert granting == []


# -- the account list ----------------------------------------------------------------


async def test_an_admin_sees_every_account_with_its_size(
    client: AsyncClient, session: AsyncSession
) -> None:
    _, first_tenant, _ = await account(client)
    auth = await admin_account(client, session)

    listed = (await client.get("/admin/tenants", headers=auth)).json()

    ids = {entry["id"] for entry in listed["value"]}
    assert str(first_tenant) in ids
    assert listed["meta"]["totalItems"] >= 2

    one = next(entry for entry in listed["value"] if entry["id"] == str(first_tenant))
    assert one["status"] == "active"
    assert one["counts"]["users"] == 1
    assert one["counts"]["agents"] == 0


async def test_accounts_can_be_searched_and_filtered(
    client: AsyncClient, session: AsyncSession
) -> None:
    _, tenant_id, _ = await account(client)
    auth = await admin_account(client, session)

    await client.put(f"/admin/tenants/{tenant_id}/status", json={"enabled": False}, headers=auth)

    disabled = (
        await client.get("/admin/tenants", params={"status": "disabled"}, headers=auth)
    ).json()["value"]
    named = (await client.get("/admin/tenants", params={"search": "acme"}, headers=auth)).json()[
        "value"
    ]

    assert [entry["id"] for entry in disabled] == [str(tenant_id)]
    assert all("acme" in entry["name"].lower() for entry in named)


async def test_an_account_can_be_found_by_email(client: AsyncClient, session: AsyncSession) -> None:
    _, tenant_id, email = await account(client)
    auth = await admin_account(client, session)

    found = (
        await client.get("/admin/accounts/by-email", params={"email": email}, headers=auth)
    ).json()["value"]

    assert found["id"] == str(tenant_id)
    assert [user["email"] for user in found["users"]] == [email]


async def test_an_unknown_email_is_a_404(client: AsyncClient, session: AsyncSession) -> None:
    auth = await admin_account(client, session)

    response = await client.get(
        "/admin/accounts/by-email", params={"email": "nobody@example.test"}, headers=auth
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


async def test_the_overview_counts_the_platform(client: AsyncClient, session: AsyncSession) -> None:
    _, tenant_id, _ = await account(client)
    auth = await admin_account(client, session)
    await client.put(f"/admin/tenants/{tenant_id}/status", json={"enabled": False}, headers=auth)

    totals = (await client.get("/admin/overview", headers=auth)).json()["value"]

    assert totals["tenants"] >= 2
    assert totals["disabledTenants"] >= 1
    assert totals["activeTenants"] == totals["tenants"] - totals["disabledTenants"]


# -- disabling an account ------------------------------------------------------------


async def test_a_disabled_account_cannot_sign_in(
    client: AsyncClient, session: AsyncSession
) -> None:
    _, tenant_id, email = await account(client)
    auth = await admin_account(client, session)

    await client.put(
        f"/admin/tenants/{tenant_id}/status",
        json={"enabled": False, "note": "Suspended pending review."},
        headers=auth,
    )

    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"
    # The reason is for whoever disabled it, not for the account holder to read back.
    assert "Suspended pending review" not in response.text


async def test_an_existing_token_stops_working_immediately(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Checked on every request, so disabling does not wait for an access token to expire."""
    tenant_auth, tenant_id, _ = await account(client)
    assert (await client.get("/agents", headers=tenant_auth)).status_code == 200

    auth = await admin_account(client, session)
    await client.put(f"/admin/tenants/{tenant_id}/status", json={"enabled": False}, headers=auth)

    response = await client.get("/agents", headers=tenant_auth)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"


async def test_re_enabling_restores_service(client: AsyncClient, session: AsyncSession) -> None:
    """Nothing is deleted, so the account works again with no further steps."""
    tenant_auth, tenant_id, _ = await account(client)
    auth = await admin_account(client, session)

    await client.put(f"/admin/tenants/{tenant_id}/status", json={"enabled": False}, headers=auth)
    restored = await client.put(
        f"/admin/tenants/{tenant_id}/status", json={"enabled": True}, headers=auth
    )

    assert restored.json()["value"]["status"] == "active"
    assert (await client.get("/agents", headers=tenant_auth)).status_code == 200


async def test_a_disabled_accounts_api_key_is_refused(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The public chat API never passes through sign-in, so it needs its own check."""
    tenant_auth, tenant_id, _ = await account(client)

    created = await client.post(
        "/agents",
        json={
            "name": "Support",
            "persona": "Helpful.",
            "modelProvider": "gemini",
            "modelSettings": {"model": "gemini-2.0-flash"},
        },
        headers=tenant_auth,
    )
    agent_id = created.json()["value"]["id"]
    await client.post(f"/agents/{agent_id}/publish", headers=tenant_auth)
    issued = await client.post(
        f"/api-keys?agentId={agent_id}", json={"name": "Widget"}, headers=tenant_auth
    )
    assert issued.status_code == 201, issued.text
    key = issued.json()["value"]["key"]

    auth = await admin_account(client, session)
    await client.put(f"/admin/tenants/{tenant_id}/status", json={"enabled": False}, headers=auth)

    response = await client.post(
        "/v1/chat/messages",
        json={"message": "Hello?"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"


async def test_an_admin_can_still_work_while_their_own_account_is_disabled(
    client: AsyncClient, session: AsyncSession
) -> None:
    """A door that can only be opened from inside is a door nobody can open."""
    auth = await admin_account(client, session)
    listed = (await client.get("/admin/tenants", headers=auth)).json()["value"]
    own = next(entry for entry in listed if entry["counts"]["users"] == 1)

    await client.put(f"/admin/tenants/{own['id']}/status", json={"enabled": False}, headers=auth)

    assert (await client.get("/admin/overview", headers=auth)).status_code == 200


# -- acting as a tenant --------------------------------------------------------------


async def test_an_admin_acts_as_a_tenant_through_the_ordinary_endpoints(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The whole CRUD story: no admin mirror of every route, just a different scope."""
    tenant_auth, tenant_id, _ = await account(client)
    await client.post(
        "/agents", json={"name": "Theirs", "persona": "Helpful."}, headers=tenant_auth
    )

    auth = await admin_account(client, session)
    acting = {**auth, "X-Tenant-Id": str(tenant_id)}

    listed = await client.get("/agents", headers=acting)
    created = await client.post(
        "/agents", json={"name": "Made by admin", "persona": "Helpful."}, headers=acting
    )

    assert [agent["name"] for agent in listed.json()["value"]] == ["Theirs"]
    assert created.status_code == 201

    # And the tenant sees what the admin did, because it was written into their tenant.
    theirs = (await client.get("/agents", headers=tenant_auth)).json()["value"]
    assert {agent["name"] for agent in theirs} == {"Theirs", "Made by admin"}


async def test_a_non_admin_sending_the_header_stays_in_their_own_tenant(
    client: AsyncClient,
) -> None:
    """The isolation invariant meeting the one feature designed to cross it.

    Ignored rather than refused: a refusal would confirm which tenant ids exist.
    """
    first_auth, first_tenant, _ = await account(client)
    await client.post(
        "/agents", json={"name": "Private", "persona": "Helpful."}, headers=first_auth
    )

    second_auth, _, _ = await account(client)
    response = await client.get(
        "/agents", headers={**second_auth, "X-Tenant-Id": str(first_tenant)}
    )

    assert response.status_code == 200
    assert response.json()["value"] == []


async def test_acting_as_an_unknown_tenant_is_a_404(
    client: AsyncClient, session: AsyncSession
) -> None:
    auth = await admin_account(client, session)

    response = await client.get("/agents", headers={**auth, "X-Tenant-Id": str(uuid.uuid4())})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TENANT_NOT_FOUND"


async def test_an_admin_can_act_as_a_disabled_tenant(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Administering an account is most needed when it is switched off."""
    _, tenant_id, _ = await account(client)
    auth = await admin_account(client, session)
    await client.put(f"/admin/tenants/{tenant_id}/status", json={"enabled": False}, headers=auth)

    response = await client.get("/agents", headers={**auth, "X-Tenant-Id": str(tenant_id)})

    assert response.status_code == 200


# -- deletion ------------------------------------------------------------------------


async def test_deleting_an_account_requires_its_name(
    client: AsyncClient, session: AsyncSession
) -> None:
    _, tenant_id, _ = await account(client)
    auth = await admin_account(client, session)

    wrong = await client.delete(
        f"/admin/tenants/{tenant_id}", params={"confirm": "not the name"}, headers=auth
    )

    assert wrong.status_code == 409
    assert wrong.json()["error"]["code"] == "TENANT_CONFIRMATION_MISMATCH"
    assert (await client.get(f"/admin/tenants/{tenant_id}", headers=auth)).status_code == 200


async def test_deleting_an_account_removes_it_and_what_it_held(
    client: AsyncClient, session: AsyncSession
) -> None:
    tenant_auth, tenant_id, _ = await account(client)
    await client.post(
        "/agents", json={"name": "Doomed", "persona": "Helpful."}, headers=tenant_auth
    )
    auth = await admin_account(client, session)
    name: str = (await client.get(f"/admin/tenants/{tenant_id}", headers=auth)).json()["value"][
        "name"
    ]

    deleted = await client.delete(
        f"/admin/tenants/{tenant_id}", params={"confirm": name}, headers=auth
    )

    assert deleted.status_code == 200
    assert (await client.get(f"/admin/tenants/{tenant_id}", headers=auth)).status_code == 404

    remaining: Any = (
        await session.execute(
            text("SELECT count(*) FROM agent WHERE tenant_id = :tenant"), {"tenant": tenant_id}
        )
    ).scalar_one()
    assert remaining == 0
