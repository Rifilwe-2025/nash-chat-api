"""The database URL must always name an async driver.

Managed hosts hand out driverless connection strings; SQLAlchemy then resolves
``postgresql://`` to psycopg2, which this project does not install. That surfaced in
production as ``ModuleNotFoundError: No module named 'psycopg2'`` during lifespan startup.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from src.shared.database.engine import create_engine, normalise_async_url


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # What Render and Fly put in DATABASE_URL — the case that broke the deploy.
        (
            "postgresql://user:pw@dpg-host.frankfurt-postgres.render.com:5432/nashdb",
            "postgresql+asyncpg://user:pw@dpg-host.frankfurt-postgres.render.com:5432/nashdb",
        ),
        # Heroku's legacy scheme.
        ("postgres://user:pw@host:5432/db", "postgresql+asyncpg://user:pw@host:5432/db"),
        # Scheme casing is not meaningful in a URL.
        ("POSTGRESQL://user:pw@host/db", "postgresql+asyncpg://user:pw@host/db"),
    ],
)
def test_driverless_postgres_urls_are_coerced_onto_asyncpg(given: str, expected: str) -> None:
    assert normalise_async_url(given) == expected


@pytest.mark.parametrize(
    "url",
    [
        # Already correct — the application.yaml default.
        "postgresql+asyncpg://postgres:qwerty@localhost:5432/nashdb",
        # An explicitly chosen different driver must not be overridden.
        "postgresql+psycopg://postgres:qwerty@localhost:5432/nashdb",
        # Not Postgres at all.
        "sqlite+aiosqlite:///./local.db",
        # Not a URL.
        "not-a-url",
    ],
)
def test_urls_that_already_name_a_driver_are_untouched(url: str) -> None:
    assert normalise_async_url(url) == url


def test_create_engine_builds_an_async_engine_from_a_driverless_url() -> None:
    """The regression test proper: this raised ModuleNotFoundError before the fix.

    ``create_async_engine`` imports the DBAPI eagerly, so constructing the engine is
    enough to prove the right driver was selected — no connection is opened.
    """
    engine = create_engine("postgresql://user:pw@host:5432/db")

    assert isinstance(engine, AsyncEngine)
    assert engine.url.drivername == "postgresql+asyncpg"
    assert engine.dialect.driver == "asyncpg"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # Neon's connection string: driverless *and* carrying libpq-only options.
        # sslmode becomes asyncpg's ssl; channel_binding is dropped as unknown to it.
        (
            "postgresql://u:pw@ep-x-pooler.c-2.us-east-2.aws.neon.tech/neondb"
            "?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:pw@ep-x-pooler.c-2.us-east-2.aws.neon.tech/neondb"
            "?ssl=require",
        ),
        # The rename applies to a URL that already named asyncpg.
        (
            "postgresql+asyncpg://u:pw@host/db?sslmode=verify-full",
            "postgresql+asyncpg://u:pw@host/db?ssl=verify-full",
        ),
        # Parameters asyncpg does understand are preserved.
        (
            "postgresql://u:pw@host/db?sslmode=require&application_name=nash",
            "postgresql+asyncpg://u:pw@host/db?application_name=nash&ssl=require",
        ),
    ],
)
def test_libpq_only_parameters_are_translated_for_asyncpg(given: str, expected: str) -> None:
    assert normalise_async_url(given) == expected


def test_libpq_parameters_survive_on_an_explicitly_chosen_sync_driver() -> None:
    """psycopg understands sslmode, so choosing it must not strip the option."""
    url = "postgresql+psycopg://u:pw@host/db?sslmode=require&channel_binding=require"

    assert normalise_async_url(url) == url


def test_create_engine_accepts_a_neon_style_url() -> None:
    """End to end: the shape that actually broke production must build an engine.

    Asserting on the connect kwargs is the part that matters — SQLAlchemy forwards
    unrecognised query parameters to the driver, so this is what would hand asyncpg a
    keyword argument it cannot take.
    """
    engine = create_engine(
        "postgresql://u:pw@ep-x-pooler.c-2.us-east-2.aws.neon.tech/neondb"
        "?sslmode=require&channel_binding=require"
    )

    assert engine.dialect.driver == "asyncpg"

    _, kwargs = engine.dialect.create_connect_args(engine.url)
    assert "sslmode" not in kwargs
    assert "channel_binding" not in kwargs
    assert kwargs["ssl"] == "require"
