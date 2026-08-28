"""Async engine and session factory.

The engine is created once at startup (see :mod:`src.core.lifespan`) and pinned to ``app.state``;
nothing reaches for a module-level global at request time.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src import configs


def create_engine(url: str | None = None) -> AsyncEngine:
    return create_async_engine(
        url or configs.DATABASE_URL,
        echo=configs.DATABASE_ECHO,
        pool_size=configs.DATABASE_POOL_SIZE,
        max_overflow=configs.DATABASE_MAX_OVERFLOW,
        pool_pre_ping=True,
        future=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
