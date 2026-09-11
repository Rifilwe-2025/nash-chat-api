"""A transaction for work that does not arrive through a FastAPI route.

Routes get their session from :func:`src.shared.database.dependencies.get_session`, which commits
when the handler returns and rolls back when it raises. Some work reaches the application another
way — a Model Context Protocol tool call is dispatched by the MCP runtime, not by FastAPI's
dependency graph — and it must keep exactly the same rule, or a failed write would be half-applied
there and nowhere else. This is that rule, as a context manager, so both paths share one definition
of where a transaction ends.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SessionFactory = async_sessionmaker[AsyncSession]


@asynccontextmanager
async def session_scope(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Commit when the block completes, roll back when it raises, always close."""
    session = factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    else:
        await session.commit()
    finally:
        await session.close()
