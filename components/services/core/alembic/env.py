"""Async Alembic env for the core.

Reads the DB URL directly from env (`DATABASE_URL` wins; else sqlite at
`RASK_BATCHES_DB` or `.cache/batches.db`). Same precedence as
`service_kit.config.Settings.resolved_database_url`, but without requiring
the viewer-input/output env vars Settings demands at construction.

Schema source: `SQLModel.metadata` — populated by importing the model
modules below.
"""

import asyncio
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlmodel import SQLModel

# Importing the batch module sets the naming convention on SQLModel.metadata
# (at module top, above any class) and then registers the Batch table.
import core.models.batch  # noqa: F401


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit
    db_path = os.environ.get("RASK_BATCHES_DB") or str(Path(".cache") / "batches.db")
    return f"sqlite+aiosqlite:///{db_path}"


config.set_main_option("sqlalchemy.url", _database_url())

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url") or ""
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {}) or {}
    connectable = async_engine_from_config(section, prefix="sqlalchemy.")
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
