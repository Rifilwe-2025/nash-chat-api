"""Removal of tokens that can never authenticate again.

Called opportunistically on login; Phase 9 moves it onto the task queue so it never sits in a
request path.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.modules.auth.domain.repositories import TokenRepository


async def purge_dead_tokens(repository: TokenRepository) -> int:
    return await repository.delete_expired(datetime.now(UTC))
