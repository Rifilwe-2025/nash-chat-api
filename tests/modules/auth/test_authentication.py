"""How protected routes respond to missing, malformed, and forged credentials."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient

from src import configs
from src.modules.auth.domain.models import TokenType
from src.modules.auth.internal.password_hasher import (
    hash_password,
    needs_rehash,
    verify_password,
)
from src.modules.auth.internal.token_provider import (
    TokenError,
    decode_token,
    hash_token,
    issue_token,
)

PROTECTED = ["/me", "/tenant", "/tenant/members"]


@pytest.mark.parametrize("path", PROTECTED)
async def test_protected_routes_reject_missing_credentials(client: AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 401
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert "value" not in body


@pytest.mark.parametrize("path", PROTECTED)
async def test_protected_routes_reject_a_garbage_token(client: AsyncClient, path: str) -> None:
    response = await client.get(path, headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


async def test_a_token_signed_with_another_secret_is_rejected(client: AsyncClient) -> None:
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "tid": str(uuid.uuid4()),
            "type": "access",
            "jti": str(uuid.uuid4()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        "a-different-secret",
        algorithm="HS256",
    )

    response = await client.get("/me", headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


async def test_a_validly_signed_but_unissued_token_is_rejected(client: AsyncClient) -> None:
    """Signature alone is never enough — the token must also exist server-side."""
    unissued = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "tid": str(uuid.uuid4()),
            "type": "access",
            "jti": str(uuid.uuid4()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        configs.AUTH_JWT_SECRET,
        algorithm=configs.AUTH_JWT_ALGORITHM,
    )

    response = await client.get("/me", headers={"Authorization": f"Bearer {unissued}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


async def test_an_expired_token_is_rejected(client: AsyncClient) -> None:
    expired = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "tid": str(uuid.uuid4()),
            "type": "access",
            "jti": str(uuid.uuid4()),
            "exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp()),
        },
        configs.AUTH_JWT_SECRET,
        algorithm=configs.AUTH_JWT_ALGORITHM,
    )

    response = await client.get("/me", headers={"Authorization": f"Bearer {expired}"})

    assert response.status_code == 401


# -- unit level ------------------------------------------------------------------


def test_passwords_hash_to_distinct_verifiable_values() -> None:
    first = hash_password("correct-horse-battery")
    second = hash_password("correct-horse-battery")

    assert first != second, "argon2 must salt each hash"
    assert verify_password("correct-horse-battery", first)
    assert verify_password("correct-horse-battery", second)


def test_password_verification_fails_closed() -> None:
    assert verify_password("anything", None) is False
    assert verify_password("anything", "") is False
    assert verify_password("anything", "not-a-hash") is False
    assert needs_rehash("not-a-hash") is True


def test_the_stored_hash_never_contains_the_token() -> None:
    issued = issue_token(uuid.uuid4(), uuid.uuid4(), TokenType.ACCESS)

    assert issued.token_hash == hash_token(issued.value)
    assert issued.value not in issued.token_hash
    assert len(issued.token_hash) == 64


def test_a_token_cannot_be_decoded_as_the_wrong_type() -> None:
    issued = issue_token(uuid.uuid4(), uuid.uuid4(), TokenType.REFRESH)

    assert decode_token(issued.value, TokenType.REFRESH)["type"] == "refresh"
    with pytest.raises(TokenError):
        decode_token(issued.value, TokenType.ACCESS)
