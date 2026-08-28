"""Signup, login, rotation, and logout over HTTP."""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient

PASSWORD = "correct-horse-battery"


def unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@example.com"


async def signup(client: AsyncClient, email: str | None = None) -> dict[str, Any]:
    response = await client.post(
        "/auth/signup",
        json={
            "email": email or unique_email(),
            "password": PASSWORD,
            "tenantName": "Acme Paints",
            "fullName": "Ada Lovelace",
        },
    )
    assert response.status_code == 201, response.text
    value: dict[str, Any] = response.json()["value"]
    return value


def auth_header(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['accessToken']}"}


async def test_signup_creates_tenant_user_and_tokens(client: AsyncClient) -> None:
    email = unique_email()

    value = await signup(client, email)

    assert value["user"]["email"] == email
    assert value["user"]["role"] == "owner"
    assert value["user"]["tenantId"]
    assert value["tokens"]["accessToken"]
    assert value["tokens"]["refreshToken"]
    assert value["tokens"]["tokenType"] == "bearer"


async def test_signup_rejects_a_duplicate_email(client: AsyncClient) -> None:
    email = unique_email()
    await signup(client, email)

    response = await client.post(
        "/auth/signup",
        json={"email": email, "password": PASSWORD, "tenantName": "Second"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EMAIL_TAKEN"


async def test_signup_rejects_a_short_password(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/signup",
        json={"email": unique_email(), "password": "short", "tenantName": "Acme"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_login_returns_a_new_pair(client: AsyncClient) -> None:
    email = unique_email()
    await signup(client, email)

    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})

    assert response.status_code == 200
    assert response.json()["value"]["tokens"]["accessToken"]


async def test_login_is_case_insensitive_on_email(client: AsyncClient) -> None:
    email = unique_email()
    await signup(client, email)

    response = await client.post("/auth/login", json={"email": email.upper(), "password": PASSWORD})

    assert response.status_code == 200


async def test_login_rejects_a_wrong_password(client: AsyncClient) -> None:
    email = unique_email()
    await signup(client, email)

    response = await client.post("/auth/login", json={"email": email, "password": "wrong-one-here"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_unknown_email_is_indistinguishable_from_a_wrong_password(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/auth/login", json={"email": unique_email(), "password": PASSWORD}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_login_revokes_previously_issued_tokens(client: AsyncClient) -> None:
    email = unique_email()
    first = await signup(client, email)

    await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    response = await client.get("/me", headers=auth_header(first["tokens"]))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_REVOKED"


async def test_refresh_rotates_the_pair(client: AsyncClient) -> None:
    value = await signup(client)

    response = await client.post(
        "/auth/refresh", json={"refreshToken": value["tokens"]["refreshToken"]}
    )

    assert response.status_code == 200
    new_tokens = response.json()["value"]["tokens"]
    assert new_tokens["accessToken"] != value["tokens"]["accessToken"]

    assert (await client.get("/me", headers=auth_header(new_tokens))).status_code == 200


async def test_a_refresh_token_works_only_once(client: AsyncClient) -> None:
    value = await signup(client)
    payload = {"refreshToken": value["tokens"]["refreshToken"]}

    assert (await client.post("/auth/refresh", json=payload)).status_code == 200
    replay = await client.post("/auth/refresh", json=payload)

    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "TOKEN_REVOKED"


async def test_an_access_token_cannot_be_used_to_refresh(client: AsyncClient) -> None:
    value = await signup(client)

    response = await client.post(
        "/auth/refresh", json={"refreshToken": value["tokens"]["accessToken"]}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


async def test_a_refresh_token_cannot_authenticate_a_request(client: AsyncClient) -> None:
    value = await signup(client)

    response = await client.get(
        "/me", headers={"Authorization": f"Bearer {value['tokens']['refreshToken']}"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


async def test_logout_revokes_every_token_immediately(client: AsyncClient) -> None:
    value = await signup(client)
    headers = auth_header(value["tokens"])

    logout = await client.post("/auth/logout", headers=headers)

    assert logout.status_code == 200
    assert logout.json()["value"]["revoked"] >= 2

    after = await client.get("/me", headers=headers)
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "TOKEN_REVOKED"

    replay = await client.post(
        "/auth/refresh", json={"refreshToken": value["tokens"]["refreshToken"]}
    )
    assert replay.status_code == 401
