"""Alembic environment.

The database URL comes from ``src.configs`` (so ``application.yaml`` stays the single source of
truth), and ``ALEMBIC_DATABASE_URL`` overrides it when migrating a non-default database — the test
harness uses that to build its own schema.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from src import configs

# Importing the models registers them on Base.metadata for autogenerate.
from src.modules.agents.domain import models as agent_models  # noqa: F401
from src.modules.analytics.domain import models as analytics_models  # noqa: F401
from src.modules.api_keys.domain import models as api_key_models  # noqa: F401
from src.modules.auth.domain import models as auth_models  # noqa: F401
from src.modules.channels.domain import models as channel_models  # noqa: F401
from src.modules.channels.whatsapp.domain import (  # noqa: F401
    models as whatsapp_models,
)
from src.modules.conversations.domain import models as conversation_models  # noqa: F401
from src.modules.knowledge_base.domain import models as kb_models  # noqa: F401
from src.modules.tenants.domain import models as tenant_models  # noqa: F401
from src.modules.tools.domain import models as tool_models  # noqa: F401
from src.shared.database.base_model import Base
from src.shared.database.engine import normalise_async_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Config value wins (the harness sets it), then the env override, then
    application.yaml."""
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return normalise_async_url(configured)
    url = os.environ.get("ALEMBIC_DATABASE_URL") or configs.DATABASE_URL  # noqa: TID251
    # Migrations run against the same managed-host URL the app gets, and
    # async_engine_from_config needs the async driver named for the same reason.
    return normalise_async_url(url)


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section: dict[str, Any] = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
