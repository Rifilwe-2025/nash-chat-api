"""Authentication business logic and transaction boundaries.

Two rules shape everything here:

* **Rotation** — issuing a new pair revokes every token the user held. A stolen refresh token stops
  working the moment the legitimate user logs in or refreshes.
* **Server-side truth** — a signature that verifies is not enough. Every token is looked up by hash
  and must still be unrevoked and unexpired, so logout takes effect immediately rather than at the
  access token's expiry.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.auth.domain.models import Token, TokenType
from src.modules.auth.domain.repositories import TokenRepository
from src.modules.auth.internal import token_provider
from src.modules.auth.internal.password_hasher import (
    hash_password,
    needs_rehash,
    verify_password,
)
from src.modules.auth.internal.token_cleanup import purge_dead_tokens
from src.modules.auth.internal.token_provider import TokenError
from src.modules.tenants.domain.models import Tenant, User, UserRole
from src.modules.tenants.domain.repositories import TenantRepository, UserRepository
from src.shared.exceptions import ConflictException, UnauthorizedException


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user: User
    tenant_id: uuid.UUID


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.tokens = TokenRepository(session)
        self.users = UserRepository(session)
        self.tenants = TenantRepository(session)

    # -- registration and sign-in -------------------------------------------

    async def signup(
        self,
        email: str,
        password: str,
        tenant_name: str,
        full_name: str | None = None,
    ) -> tuple[User, TokenPair]:
        """Create the tenant and its first user together — a user cannot exist without a tenant."""
        if await self.users.email_exists(email):
            raise ConflictException(
                "An account with that email already exists.", code="EMAIL_TAKEN"
            )

        tenant = await self.tenants.add(Tenant(name=tenant_name))
        user = await self.users.add(
            User(
                tenant_id=tenant.id,
                email=email.lower(),
                full_name=full_name,
                password_hash=hash_password(password),
                role=UserRole.OWNER,
            )
        )
        return user, await self._issue_pair(user)

    async def login(self, email: str, password: str) -> tuple[User, TokenPair]:
        user = await self.users.get_by_email(email)

        # Verify even when the user is missing, so a wrong email and a wrong password cost the
        # same time and cannot be told apart by an attacker enumerating accounts.
        password_ok = verify_password(password, user.password_hash if user else None)
        if user is None or not password_ok:
            raise UnauthorizedException(
                "Email or password is incorrect.", code="INVALID_CREDENTIALS"
            )

        if user.password_hash and needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        await purge_dead_tokens(self.tokens)
        return user, await self._issue_pair(user)

    async def refresh(self, refresh_token: str) -> tuple[User, TokenPair]:
        claims = self._decode(refresh_token, TokenType.REFRESH)
        stored = await self._load_usable(refresh_token)
        user = await self._load_user(token_provider.subject_of(claims))

        if stored.user_id != user.id:
            raise UnauthorizedException("Token does not match its subject.", code="INVALID_TOKEN")

        return user, await self._issue_pair(user)

    async def logout(self, access_token: str) -> int:
        """Revoke every token the caller holds, not just the one presented."""
        claims = self._decode(access_token, TokenType.ACCESS)
        await self._load_usable(access_token)
        user_id = token_provider.subject_of(claims)
        return await self.tokens.revoke_all_for_user(user_id, datetime.now(UTC))

    # -- request authentication ---------------------------------------------

    async def authenticate(self, access_token: str) -> AuthenticatedUser:
        claims = self._decode(access_token, TokenType.ACCESS)
        stored = await self._load_usable(access_token)
        user = await self._load_user(token_provider.subject_of(claims))

        if stored.user_id != user.id:
            raise UnauthorizedException("Token does not match its subject.", code="INVALID_TOKEN")

        return AuthenticatedUser(user=user, tenant_id=user.tenant_id)

    # -- internals -----------------------------------------------------------

    async def _issue_pair(self, user: User) -> TokenPair:
        now = datetime.now(UTC)
        await self.tokens.revoke_all_for_user(user.id, now)

        access = token_provider.issue_token(user.id, user.tenant_id, TokenType.ACCESS)
        refresh = token_provider.issue_token(user.id, user.tenant_id, TokenType.REFRESH)

        for issued, token_type in ((access, TokenType.ACCESS), (refresh, TokenType.REFRESH)):
            await self.tokens.add(
                Token(
                    user_id=user.id,
                    token_hash=issued.token_hash,
                    token_type=token_type,
                    expires_at=issued.expires_at,
                )
            )

        return TokenPair(
            access_token=access.value,
            refresh_token=refresh.value,
            expires_at=access.expires_at,
        )

    def _decode(self, token: str, expected: TokenType) -> dict[str, object]:
        try:
            return token_provider.decode_token(token, expected)
        except TokenError as exc:
            raise UnauthorizedException(str(exc), code="INVALID_TOKEN") from exc

    async def _load_usable(self, token: str) -> Token:
        stored = await self.tokens.get_by_hash(token_provider.hash_token(token))
        if stored is None:
            raise UnauthorizedException("Token is not recognised.", code="INVALID_TOKEN")
        if not stored.is_usable(datetime.now(UTC)):
            raise UnauthorizedException(
                "Token has been revoked or has expired.", code="TOKEN_REVOKED"
            )
        return stored

    async def _load_user(self, user_id: uuid.UUID) -> User:
        user = await self.users.get(user_id)
        if user is None:
            raise UnauthorizedException("Account no longer exists.", code="INVALID_TOKEN")
        return user
