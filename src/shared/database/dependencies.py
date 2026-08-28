"""Request-scoped database session.

The dependency owns the transaction boundary: it commits when the handler returns cleanly and rolls
back on any exception. Repositories therefore only ever ``flush()`` — see
:mod:`src.shared.database.repository`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    )
    if factory is None:  # pragma: no cover - misconfigured application
        raise RuntimeError("session_factory is not configured on app.state")
    return factory


async def get_session(
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AsyncIterator[AsyncSession]:
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


SessionDep = Annotated[AsyncSession, Depends(get_session)]
