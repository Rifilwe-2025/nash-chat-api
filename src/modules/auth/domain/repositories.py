"""Token reads and bulk revocation — every ``select(...)`` for this module lives here."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, or_, update

from src.modules.auth.domain.models import Token, TokenType
from src.shared.database.repository import BaseRepository


class TokenRepository(BaseRepository[Token]):
    model = Token

    async def get_by_hash(self, token_hash: str) -> Token | None:
        query = self._base_query().where(Token.token_hash == token_hash)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def revoke_all_for_user(self, user_id: uuid.UUID, now: datetime) -> int:
        """Rotation: issuing a new pair invalidates everything issued before it."""
        result = await self.session.execute(
            update(Token)
            .where(Token.user_id == user_id, Token.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await self.session.flush()
        return result.rowcount or 0

    async def revoke(self, token: Token, now: datetime) -> Token:
        token.revoked_at = now
        await self.session.flush()
        return token

    async def list_active_for_user(self, user_id: uuid.UUID, now: datetime) -> list[Token]:
        query = self._base_query().where(
            Token.user_id == user_id,
            Token.revoked_at.is_(None),
            Token.expires_at > now,
        )
        return list((await self.session.execute(query)).scalars().all())

    async def delete_expired(self, now: datetime) -> int:
        """Prune rows that can never authenticate again."""
        result = await self.session.execute(
            delete(Token).where(or_(Token.expires_at <= now, Token.revoked_at.isnot(None)))
        )
        await self.session.flush()
        return result.rowcount or 0

    async def count_for_user(self, user_id: uuid.UUID, token_type: TokenType) -> int:
        query = self._base_query().where(Token.user_id == user_id, Token.token_type == token_type)
        return len(list((await self.session.execute(query)).scalars().all()))
