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
from src.modules.tenants.domain.models import User
from src.modules.tenants.domain.services import TenantService
from src.shared.exceptions import ConflictException, ForbiddenException, UnauthorizedException


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
        # Service to service: auth owns passwords, tokens and sessions; the tenant and user rows
        # belong to the tenants module, and are reached through its service rather than its
        # repositories (the layering rule in CLAUDE.md).
        self.accounts = TenantService(session)

    # -- registration and sign-in -------------------------------------------

    async def signup(
        self,
        email: str,
        password: str,
        tenant_name: str,
        full_name: str | None = None,
    ) -> tuple[User, TokenPair]:
        """Create the tenant and its first user together — a user cannot exist without a tenant."""
        if await self.accounts.email_taken(email):
            raise ConflictException(
                "An account with that email already exists.", code="EMAIL_TAKEN"
            )

        _, user = await self.accounts.register(
            tenant_name=tenant_name,
            email=email,
            password_hash=hash_password(password),
            full_name=full_name,
        )
        return user, await self._issue_pair(user)

    async def provision_account(
        self,
        email: str,
        password: str,
        tenant_name: str,
        full_name: str | None = None,
        is_platform_admin: bool = False,
        must_change_password: bool = False,
    ) -> User:
        """Create an account somebody *else* chose the password for.

        Sign-up with no tokens issued, because nobody is signing in — this is a deployment creating
        the first platform administrator from its environment. It lives in the auth module for one
        reason: passwords are hashed here and nowhere else, and a caller that had to hash its own
        would be a second place that could get it wrong.

        ``must_change_password`` is the honest half of it. A password the account holder did not
        choose is a password somebody else knows, so the account gets to change it and do nothing
        else until it has.
        """
        if await self.accounts.email_taken(email):
            raise ConflictException(
                "An account with that email already exists.", code="EMAIL_TAKEN"
            )

        _, user = await self.accounts.register(
            tenant_name=tenant_name,
            email=email,
            password_hash=hash_password(password),
            full_name=full_name,
            is_platform_admin=is_platform_admin,
            must_change_password=must_change_password,
        )
        return user

    async def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> tuple[User, TokenPair]:
        """Replace a password, and end every session that was using the old one.

        The current password is required even though the caller is already authenticated. An access
        token is a bearer credential — a borrowed laptop, a copied header — and knowing the password
        is the only evidence that the person changing it is the person who owns it.

        A fresh pair comes back because ``_issue_pair`` revokes everything issued before: changing a
        password because it may be known ends the sessions that may be using it, which is most of
        the point.
        """
        if not verify_password(current_password, user.password_hash):
            raise UnauthorizedException(
                "The current password is incorrect.", code="INVALID_CREDENTIALS"
            )

        if verify_password(new_password, user.password_hash):
            raise ConflictException(
                "The new password must be different from the current one.",
                code="PASSWORD_UNCHANGED",
            )

        user.password_hash = hash_password(new_password)
        # Cleared here rather than anywhere else: this is the only path that sets a password the
        # account holder chose, which is exactly what the flag was waiting for.
        user.must_change_password = False
        await self.session.flush()

        return user, await self._issue_pair(user)

    async def login(self, email: str, password: str) -> tuple[User, TokenPair]:
        user = await self.accounts.find_by_email(email)

        # Verify even when the user is missing, so a wrong email and a wrong password cost the
        # same time and cannot be told apart by an attacker enumerating accounts.
        password_ok = verify_password(password, user.password_hash if user else None)
        if user is None or not password_ok:
            raise UnauthorizedException(
                "Email or password is incorrect.", code="INVALID_CREDENTIALS"
            )

        # After the password check, never before: refusing a disabled account earlier would tell an
        # attacker which addresses have accounts, which is the thing the constant-time check above
        # exists to hide.
        self._assert_account_enabled(user)

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

        # Checked on every request rather than only at sign-in, so disabling an account takes effect
        # immediately instead of whenever the holder's access token happens to expire.
        self._assert_account_enabled(user)

        return AuthenticatedUser(user=user, tenant_id=user.tenant_id)

    @staticmethod
    def _assert_account_enabled(user: User) -> None:
        """Refuse a user whose tenant has been disabled.

        Platform staff are exempt. They are the people who re-enable an account, and locking them
        out of the tool that does it — because somebody disabled the tenant they happen to belong
        to — would be a door that can only be opened from inside.

        The message names no reason. Why an account was disabled is a note for whoever disabled it,
        not a line the account holder gets to read back.
        """
        if user.is_platform_admin or user.tenant.is_active:
            return
        raise ForbiddenException(
            "This account has been disabled. Contact support to have it restored.",
            code="ACCOUNT_DISABLED",
        )

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
        user = await self.accounts.find_user(user_id)
        if user is None:
            raise UnauthorizedException("Account no longer exists.", code="INVALID_TOKEN")
        return user
