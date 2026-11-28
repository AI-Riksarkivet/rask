"""The catalog's database schema: which revision this image was built for, which one the database holds, and the
migration that brings it there (ADR 0230 §11).

The release's migration Job runs :func:`migrate` (``python -m catalog.schema migrate``) as the migrator role: every
pending revision in ONE transaction, behind a transaction-scoped advisory lock, so two runs at once apply each revision
once and a failed run leaves the schema where it was. A catalog pod waits for the image's revision (:func:`wait`, its
init step) and checks again before it serves (:func:`require_schema`); a schema behind or ahead of the image refuses
either, since no code of this image is right for it. There are no down migrations.
"""

import asyncio
import time
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from lance_namespace import ServiceUnavailableError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, column, func, select, table
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema

from catalog.core.database import Database, DatabaseSettings, connector, open_database
from catalog.core.rows import SCHEMA


__all__ = [
    "MIGRATION_ROLES",
    "MigrateSettings",
    "MigrationRoles",
    "SchemaMismatchError",
    "SchemaSettings",
    "current",
    "head",
    "migrate",
    "require_schema",
    "wait",
]

#: Where the revisions live, read from the installed package.
_SCRIPT_LOCATION: Final = "catalog:migrations"
#: The key of the transaction-scoped advisory lock every migration run takes first: any fixed bigint no other code of
#: this database takes.
_MIGRATION_LOCK: Final = 0x7261736B5F6D6967
#: How often a waiting pod and a migrator still waiting for its role ask again.
_POLL_SECONDS: Final = 2.0
#: The key of :class:`MigrationRoles` in Alembic's ``config.attributes``, which ``env.py`` hands to each revision.
MIGRATION_ROLES: Final = "roles"
#: What the migration Job's connections report to the server, apart from the catalog pods' own.
_MIGRATE_APPLICATION_NAME: Final = "rask-catalog-migrate"


class SchemaSettings(DatabaseSettings):
    """The pod's wait for its schema (``python -m catalog.schema wait``)."""

    #: How long a pod waits for a schema still behind before it refuses; inside the release's own timeout.
    database_schema_wait_seconds: float = Field(default=600, gt=0, alias="RASK_DATABASE_SCHEMA_WAIT_SECONDS")


class MigrateSettings(DatabaseSettings):
    """The migration Job (``python -m catalog.schema migrate``), connecting as the migrator role."""

    #: How long the migrator retries a connection the server refuses: in development its role appears once the
    #: database's provisioning step has run.
    database_connect_wait_seconds: float = Field(default=600, gt=0, alias="RASK_DATABASE_CONNECT_WAIT_SECONDS")
    database_catalog_role: str = Field(alias="RASK_DATABASE_CATALOG_ROLE")
    database_maintenance_role: str = Field(alias="RASK_DATABASE_MAINTENANCE_ROLE")


class MigrationRoles(BaseModel):
    """The roles a revision grants to, as the platform names them."""

    model_config = ConfigDict(frozen=True)

    catalog: str
    maintenance: str


class SchemaMismatchError(RuntimeError):
    """The database's schema is not the revision this image was built for."""


class SchemaAheadError(SchemaMismatchError):
    """The database's schema is at a revision this image does not know: a newer release migrated it."""


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", _SCRIPT_LOCATION)
    return config


def head() -> str:
    """The revision this image was built for: its newest."""
    revision = ScriptDirectory.from_config(_config()).get_current_head()
    if revision is None:
        raise SchemaMismatchError("this image carries no revision of the catalog's database schema")
    return revision


def _known() -> frozenset[str]:
    return frozenset(script.revision for script in ScriptDirectory.from_config(_config()).walk_revisions())


async def current(database: Database) -> str | None:
    """The revision the database holds, or ``None`` before the first migration.

    Raises:
        ServiceUnavailableError: The database did not answer.
    """
    async with database.reading() as session:
        return await _held(await session.connection())


async def _held(connection: AsyncConnection) -> str | None:
    """The revision the database ``connection`` reaches holds, or ``None`` before the first migration."""
    found = await connection.execute(select(func.to_regclass(f"{SCHEMA}.alembic_version").is_not(None)))
    if found.scalar_one() is not True:
        return None
    return (await connection.execute(select(column("version_num")).select_from(table("alembic_version", schema=SCHEMA)))).scalar_one_or_none()


def _ahead(held: str | None, newest: str) -> SchemaAheadError | None:
    """What refuses a schema at ``held``, a revision this image does not know; ``None`` for one it knows or none."""
    if held is None or held in _known():
        return None
    return SchemaAheadError(
        f"the catalog's database schema is at revision {held}, which this image does not know (its newest is {newest}): "
        "a newer release migrated this database; run a release at least as new"
    )


def _mismatch(held: str | None, newest: str) -> SchemaMismatchError | None:
    """What refuses a schema at ``held`` for an image whose newest revision is ``newest``; ``None`` when they agree."""
    if held == newest:
        return None
    if (ahead := _ahead(held, newest)) is not None:
        return ahead
    return SchemaMismatchError(
        f"the catalog's database schema is at revision {held or 'none'} and this image needs {newest}: the migration Job has not applied it"
    )


async def _refusal(database: Database, newest: str) -> SchemaMismatchError | ServiceUnavailableError | None:
    """What refuses the database's schema now: a mismatch, or the database not answering; ``None`` at ``newest``."""
    try:
        return _mismatch(await current(database), newest)
    except ServiceUnavailableError as unanswered:
        return unanswered


async def require_schema(database: Database) -> None:
    """Return when the database holds this image's revision.

    Raises:
        SchemaMismatchError: The schema is behind this image's revision, or at one this image does not know.
        ServiceUnavailableError: The database did not answer.
    """
    if (mismatch := _mismatch(await current(database), head())) is not None:
        raise mismatch


async def wait(settings: SchemaSettings) -> str:
    """Wait until the database holds this image's revision, and return it; a pod's init step. A database that does not
    answer yet is asked again, as a schema still behind is, and so is a login it refuses, which reaches the catalog as a
    failed connection (:mod:`catalog.core.database`) and which in development heals once the role is provisioned. A
    statement the database refuses (a privilege, a name) is not asked again.

    Raises:
        SchemaMismatchError: The schema is at a revision this image does not know, at once, or still behind once
            ``database_schema_wait_seconds`` have passed.
        ServiceUnavailableError: The database still did not answer once ``database_schema_wait_seconds`` had passed.
        sqlalchemy.exc.DBAPIError: The database refused the read (a privilege, a name it does not know), at once.
    """
    database = open_database(settings)
    deadline = time.monotonic() + settings.database_schema_wait_seconds
    newest = head()
    try:
        while (refusal := await _refusal(database, newest)) is not None:
            # Retrying never makes a newer schema older.
            if isinstance(refusal, SchemaAheadError) or time.monotonic() >= deadline:
                raise refusal
            await asyncio.sleep(_POLL_SECONDS)
    finally:
        await database.dispose()
    return newest


def _upgrade(connection: Connection, roles: MigrationRoles) -> None:
    config = _config()
    config.attributes["connection"] = connection
    config.attributes[MIGRATION_ROLES] = roles
    command.upgrade(config, "head")


async def _connect_patiently(engine: AsyncEngine, wait_seconds: float) -> AsyncConnection:
    """A connection, retried while the server refuses it until ``wait_seconds`` pass."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            return await engine.connect()
        except sa_exc.OperationalError:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(_POLL_SECONDS)


async def _apply(connection: AsyncConnection, roles: MigrationRoles) -> None:
    """Every pending revision on ``connection``, in one transaction behind the advisory lock.

    Raises:
        SchemaAheadError: The database holds a revision this image does not know; nothing was applied.
    """
    async with connection.begin():
        await connection.execute(select(func.pg_advisory_xact_lock(_MIGRATION_LOCK)))
        # Alembic writes its version table into the schema before the first revision runs.
        await connection.execute(CreateSchema(SCHEMA, if_not_exists=True))
        if (ahead := _ahead(await _held(connection), head())) is not None:
            raise ahead
        await connection.run_sync(_upgrade, roles)


async def migrate(settings: MigrateSettings) -> str:
    """Apply every pending revision as the migrator, in one transaction behind the advisory lock, and return the revision
    the database holds after: a run that finds the schema at its head applies nothing.

    Raises:
        SchemaAheadError: The database holds a revision this image does not know; nothing was applied.
    """
    connect = connector(settings, application_name=_MIGRATE_APPLICATION_NAME)
    engine = create_async_engine("postgresql+psycopg://", async_creator=connect, poolclass=NullPool)
    roles = MigrationRoles(catalog=settings.database_catalog_role, maintenance=settings.database_maintenance_role)
    try:
        connection = await _connect_patiently(engine, settings.database_connect_wait_seconds)
        try:
            await _apply(connection, roles)
        finally:
            await connection.close()
    finally:
        await engine.dispose()
    return head()
