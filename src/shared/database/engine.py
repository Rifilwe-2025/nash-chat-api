"""Async engine and session factory.

The engine is created once at startup (see :mod:`src.core.lifespan`) and pinned to ``app.state``;
nothing reaches for a module-level global at request time.
"""

from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src import configs

# Managed Postgres hosts hand out libpq-shaped connection strings — Render and Fly use
# ``postgresql://``, Heroku still emits the legacy ``postgres://``, and Neon appends libpq SSL
# options. This project is async-only on asyncpg, and asyncpg speaks its own connection API
# rather than libpq's, so such a URL needs two corrections before it can be used:
#
#   1. A driverless ``postgresql://`` resolves to psycopg2, which is deliberately not
#      installed — surfacing at startup as ``ModuleNotFoundError: No module named 'psycopg2'``.
#   2. SQLAlchemy forwards unknown query parameters to the driver verbatim, so libpq-only
#      options reach ``asyncpg.connect()`` as unexpected keyword arguments.
#
# Both failures happen at boot, a long way from the environment variable that caused them.
_POSTGRES_BACKENDS = frozenset({"postgres", "postgresql"})
_ASYNC_DRIVER = "asyncpg"

# libpq's ``sslmode`` is asyncpg's ``ssl``; the accepted values (disable/allow/prefer/require/
# verify-ca/verify-full) are identical, so this is a rename, not a translation.
_SSL_PARAM_RENAMES = {"sslmode": "ssl"}

# Meaningful to libpq, unknown to asyncpg. SCRAM channel binding is negotiated by asyncpg
# internally, so dropping the request for it does not weaken the connection.
_PARAMS_UNKNOWN_TO_ASYNCPG = frozenset({"channel_binding"})


def normalise_async_url(url: str) -> str:
    """Rewrite a managed-host Postgres URL into one asyncpg can actually consume.

    A URL naming a driver other than asyncpg is returned untouched, so choosing a different
    one stays possible and libpq parameters that are valid there survive. Anything that is
    not Postgres, and anything unparseable, is passed through unchanged.
    """
    try:
        parsed = make_url(url)
    except ArgumentError:
        return url

    backend, _, driver = parsed.drivername.partition("+")
    if backend.lower() not in _POSTGRES_BACKENDS:
        return url
    if driver and driver.lower() != _ASYNC_DRIVER:
        return url

    query = {
        _SSL_PARAM_RENAMES.get(key, key): value
        for key, value in parsed.query.items()
        if key not in _PARAMS_UNKNOWN_TO_ASYNCPG
    }
    normalised = parsed.set(drivername=f"postgresql+{_ASYNC_DRIVER}", query=query)
    return normalised.render_as_string(hide_password=False)


def create_engine(url: str | None = None) -> AsyncEngine:
    return create_async_engine(
        normalise_async_url(url or configs.DATABASE_URL),
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
