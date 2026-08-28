"""JWT issuing, decoding, and the hash under which a token is persisted.

Tokens are JWTs so their claims travel with them, *and* every issued token is recorded server-side
as a SHA-256 hash. The stored hash is what makes revocation real: logout and refresh-rotation mark
rows revoked, and a token whose row is missing, revoked, or expired is rejected even though its
signature still verifies.

SHA-256 (not Argon2) is correct here — the token is a 256-bit random-ish secret, not a
human-chosen password, so it needs no work factor, and lookups must be fast and by exact value.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from src import configs
from src.modules.auth.domain.models import TokenType


class TokenError(Exception):
    """The token is malformed, expired, or not the type the caller expected."""


@dataclass(frozen=True, slots=True)
class IssuedToken:
    value: str
    token_hash: str
    expires_at: datetime
    jti: uuid.UUID


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ttl(token_type: TokenType) -> timedelta:
    if token_type is TokenType.ACCESS:
        return timedelta(minutes=configs.AUTH_ACCESS_TTL_MINUTES)
    return timedelta(days=configs.AUTH_REFRESH_TTL_DAYS)


def issue_token(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    token_type: TokenType,
) -> IssuedToken:
    now = datetime.now(UTC)
    expires_at = now + _ttl(token_type)
    jti = uuid.uuid4()

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "type": token_type.value,
        "jti": str(jti),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    value = jwt.encode(payload, configs.AUTH_JWT_SECRET, algorithm=configs.AUTH_JWT_ALGORITHM)
    return IssuedToken(
        value=value,
        token_hash=hash_token(value),
        expires_at=expires_at,
        jti=jti,
    )


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Verify signature and expiry, then confirm the token is being used for its own purpose."""
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            configs.AUTH_JWT_SECRET,
            algorithms=[configs.AUTH_JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("token is invalid") from exc

    if claims.get("type") != expected_type.value:
        raise TokenError(f"expected a {expected_type.value} token")

    return claims


def subject_of(claims: dict[str, Any]) -> uuid.UUID:
    try:
        return uuid.UUID(str(claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise TokenError("token subject is missing or malformed") from exc
