"""The bootstrap administrator, and the password change it is forced into.

The feature exists to solve a chicken and egg problem — a fresh deployment has nobody who can
administer it — and the risk it introduces is a shared password sitting in a configuration file. So
what these tests pin down is not "an account gets created" but the three properties that make
creating one safe:

* it is created **once**, and a restart never resets a live account's password back to the file;
* the account can do **nothing** until the password is changed, not merely be advised to change it;
* changing it **ends the sessions** that were using the old one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.admin.internal.bootstrap import (
    ensure_bootstrap_admin,
    warn_if_handover_password_unchanged,
)
from src.modules.tenants.domain.services import TenantService

EMAIL = "platform-admin@example.com"
HANDOVER = "change-me-on-first-login"
CHOSEN = "a-much-better-passphrase"


@pytest.fixture
def configured(config_override: Callable[..., None]) -> None:
    config_override(
        ADMIN_BOOTSTRAP_EMAIL=EMAIL,
        ADMIN_BOOTSTRAP_PASSWORD=HANDOVER,
        ADMIN_BOOTSTRAP_NAME="Platform Administrator",
        ADMIN_BOOTSTRAP_TENANT_NAME="Platform",
    )


async def sign_in(client: AsyncClient, password: str = HANDOVER) -> Any:
    return await client.post("/auth/login", json={"email": EMAIL, "password": password})


def header(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['accessToken']}"}


# -- creating it ---------------------------------------------------------------------


async def test_it_creates_a_platform_admin_that_must_change_its_password(
    session: AsyncSession, configured: None
) -> None:
    result = await ensure_bootstrap_admin(session)

    assert result.created is True
    user = await TenantService(session).find_by_email(EMAIL)
    assert user is not None
    assert user.is_platform_admin is True
    assert user.must_change_password is True
    assert user.tenant.name == "Platform"
    # The password is stored the way every other password is, never in the clear.
    assert user.password_hash is not None and HANDOVER not in user.password_hash


async def test_nothing_happens_when_it_is_not_configured(
    session: AsyncSession, config_override: Callable[..., None]
) -> None:
    """A deployment that has not asked for an administrator does not get one.

    The environment is cleared explicitly rather than assumed empty: a developer's own `.env` sets
    these, and a test that passes only on a machine without one is a test that fails at random.
    """
    config_override(ADMIN_BOOTSTRAP_EMAIL="", ADMIN_BOOTSTRAP_PASSWORD="")

    result = await ensure_bootstrap_admin(session)

    assert result.created is False
    assert result.reason == "not configured"


async def test_an_address_that_cannot_sign_in_is_refused(
    session: AsyncSession, config_override: Callable[..., None]
) -> None:
    """`.local` and `.test` are rejected by the login endpoint, so an account using one would be an
    administrator nobody could authenticate as."""
    config_override(
        ADMIN_BOOTSTRAP_EMAIL="admin@nashpaints.local", ADMIN_BOOTSTRAP_PASSWORD=HANDOVER
    )

    result = await ensure_bootstrap_admin(session)

    assert result.created is False
    assert result.reason == "email invalid"


async def test_a_half_configured_environment_creates_nothing(
    session: AsyncSession, config_override: Callable[..., None]
) -> None:
    config_override(ADMIN_BOOTSTRAP_EMAIL=EMAIL, ADMIN_BOOTSTRAP_PASSWORD="")

    assert (await ensure_bootstrap_admin(session)).created is False
    assert await TenantService(session).find_by_email(EMAIL) is None


async def test_a_short_handover_password_is_refused(
    session: AsyncSession, config_override: Callable[..., None]
) -> None:
    """A handover password short enough to guess is worse than no bootstrap account."""
    config_override(ADMIN_BOOTSTRAP_EMAIL=EMAIL, ADMIN_BOOTSTRAP_PASSWORD="short")

    result = await ensure_bootstrap_admin(session)

    assert result.created is False
    assert result.reason == "password too short"
    assert await TenantService(session).find_by_email(EMAIL) is None


async def test_running_it_twice_creates_one_account(
    session: AsyncSession, configured: None
) -> None:
    await ensure_bootstrap_admin(session)
    second = await ensure_bootstrap_admin(session)

    assert second.created is False
    assert second.reason == "already exists"

    count = (
        await session.execute(
            text('SELECT count(*) FROM "user" WHERE lower(email) = lower(:email)'),
            {"email": EMAIL},
        )
    ).scalar_one()
    assert count == 1


async def test_a_restart_does_not_reset_a_changed_password(
    client: AsyncClient, session: AsyncSession, configured: None
) -> None:
    """The failure this guards against: a container restart handing a live account the file's
    password back at three in the morning."""
    await ensure_bootstrap_admin(session)
    tokens = (await sign_in(client)).json()["value"]["tokens"]
    await client.post(
        "/auth/password",
        json={"currentPassword": HANDOVER, "newPassword": CHOSEN},
        headers=header(tokens),
    )

    await ensure_bootstrap_admin(session)

    assert (await sign_in(client, HANDOVER)).status_code == 401
    assert (await sign_in(client, CHOSEN)).status_code == 200


async def test_the_startup_warning_is_survivable(session: AsyncSession, configured: None) -> None:
    """It must not raise: a boot-time warning that throws is an outage."""
    await ensure_bootstrap_admin(session)

    await warn_if_handover_password_unchanged(session)


# -- being forced to change it -------------------------------------------------------


async def test_it_can_sign_in_and_is_told_it_must_change(
    client: AsyncClient, session: AsyncSession, configured: None
) -> None:
    await ensure_bootstrap_admin(session)

    response = await sign_in(client)

    assert response.status_code == 200
    user = response.json()["value"]["user"]
    assert user["mustChangePassword"] is True
    assert user["isPlatformAdmin"] is True


async def test_it_can_do_nothing_else_until_it_has(
    client: AsyncClient, session: AsyncSession, configured: None
) -> None:
    """Forced, not advised. Every other route is closed, including the admin ones."""
    await ensure_bootstrap_admin(session)
    auth = header((await sign_in(client)).json()["value"]["tokens"])

    refused = [
        await client.get("/agents", headers=auth),
        await client.get("/admin/tenants", headers=auth),
        await client.get("/me", headers=auth),
    ]

    assert [response.status_code for response in refused] == [403, 403, 403]
    assert refused[0].json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"


async def test_changing_the_password_opens_the_rest_of_the_api(
    client: AsyncClient, session: AsyncSession, configured: None
) -> None:
    await ensure_bootstrap_admin(session)
    auth = header((await sign_in(client)).json()["value"]["tokens"])

    changed = await client.post(
        "/auth/password",
        json={"currentPassword": HANDOVER, "newPassword": CHOSEN},
        headers=auth,
    )

    assert changed.status_code == 200
    assert changed.json()["value"]["user"]["mustChangePassword"] is False

    new_auth = header(changed.json()["value"]["tokens"])
    assert (await client.get("/admin/tenants", headers=new_auth)).status_code == 200


async def test_changing_the_password_ends_the_old_session(
    client: AsyncClient, session: AsyncSession, configured: None
) -> None:
    """A password is changed because it may be known; the sessions using it should stop."""
    await ensure_bootstrap_admin(session)
    old_auth = header((await sign_in(client)).json()["value"]["tokens"])

    await client.post(
        "/auth/password",
        json={"currentPassword": HANDOVER, "newPassword": CHOSEN},
        headers=old_auth,
    )

    assert (await client.get("/admin/tenants", headers=old_auth)).status_code == 401


async def test_the_current_password_is_required(
    client: AsyncClient, session: AsyncSession, configured: None
) -> None:
    """An access token can be borrowed; knowing the password is the evidence of ownership."""
    await ensure_bootstrap_admin(session)
    auth = header((await sign_in(client)).json()["value"]["tokens"])

    response = await client.post(
        "/auth/password",
        json={"currentPassword": "not-the-handover", "newPassword": CHOSEN},
        headers=auth,
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_the_new_password_must_differ(
    client: AsyncClient, session: AsyncSession, configured: None
) -> None:
    """Otherwise the flag could be cleared while the file's password still works."""
    await ensure_bootstrap_admin(session)
    auth = header((await sign_in(client)).json()["value"]["tokens"])

    response = await client.post(
        "/auth/password",
        json={"currentPassword": HANDOVER, "newPassword": HANDOVER},
        headers=auth,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PASSWORD_UNCHANGED"


async def test_a_short_new_password_is_rejected(
    client: AsyncClient, session: AsyncSession, configured: None
) -> None:
    await ensure_bootstrap_admin(session)
    auth = header((await sign_in(client)).json()["value"]["tokens"])

    response = await client.post(
        "/auth/password", json={"currentPassword": HANDOVER, "newPassword": "short"}, headers=auth
    )

    assert response.status_code == 422


async def test_an_ordinary_user_can_change_their_password_too(client: AsyncClient) -> None:
    """The endpoint is not admin-only — it is the ordinary way anybody changes a password."""
    from tests.modules.auth.test_auth_flow import PASSWORD, signup

    value = await signup(client)
    auth = header(value["tokens"])

    changed = await client.post(
        "/auth/password",
        json={"currentPassword": PASSWORD, "newPassword": CHOSEN},
        headers=auth,
    )

    assert changed.status_code == 200
    assert (
        await client.get("/agents", headers=header(changed.json()["value"]["tokens"]))
    ).status_code == 200
