"""The catalog's database: one async engine a process, and one transaction a request path (ADR 0230 §8, §10).

The engine is private to this module. A door reaches the database only through :meth:`Database.reading` and
:meth:`Database.writing`, each one transaction on one pooled connection, and passes the session it yields down to every
query of its path, so no path holds two connections at once: a second transaction opened while one is open in the same
context is refused (:class:`SecondTransactionError`). A door leaves its block, and so commits, before it announces and
before it answers, which is why the session is no FastAPI dependency (a ``yield`` dependency would commit after the
response is sent).

Each NEW connection reads the role's ``username`` and ``password`` from the credential directory the secret store
renders, so a credential replaced there is the next connection's, with no restart; a connection is recycled within the
credential's life (``pool_recycle``).
"""

from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Final

import psycopg
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ServiceUnavailableError
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import TextClause, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession


__all__ = ["Database", "DatabaseSettings", "SecondTransactionError", "connector", "open_database"]

#: What every connection the catalog's pods open reports to the server, so an operator tells its sessions apart.
APPLICATION_NAME: Final = "rask-catalog"

#: Whether a transaction of this module is open in the running context: the guard a second open is refused by.
_OPEN: Final[ContextVar[bool]] = ContextVar("catalog_database_transaction_open", default=False)

_READ_ONLY: Final = (text("SET TRANSACTION READ ONLY"),)


class DatabaseSettings(BaseSettings):
    """Where the catalog's database is and how its pool is sized, from ``RASK_DATABASE_*``.

    The host, the database and the credential directory are required: there is no catalog without its database.
    """

    model_config = SettingsConfigDict(validate_by_name=True, validate_by_alias=True, env_prefix="RASK_", extra="ignore")

    database_host: str = Field(alias="RASK_DATABASE_HOST")
    database_port: int = Field(default=5432, alias="RASK_DATABASE_PORT")
    database_name: str = Field(alias="RASK_DATABASE_NAME")
    #: Holds the files ``username`` and ``password``, read at every new connection.
    database_credentials_dir: Path = Field(alias="RASK_DATABASE_CREDENTIALS_DIR")
    database_pool_size: int = Field(default=4, ge=1, alias="RASK_DATABASE_POOL_SIZE")
    #: How long a transaction waits for a pooled connection before the door answers unavailable.
    database_acquire_timeout_seconds: float = Field(default=5, gt=0, alias="RASK_DATABASE_ACQUIRE_TIMEOUT_SECONDS")
    #: A connection older than this is replaced at its next checkout, inside the credential's life.
    database_recycle_seconds: int = Field(default=1800, gt=0, alias="RASK_DATABASE_RECYCLE_SECONDS")
    database_connect_timeout_seconds: int = Field(default=5, gt=0, alias="RASK_DATABASE_CONNECT_TIMEOUT_SECONDS")


class _Credential(BaseModel):
    model_config = ConfigDict(frozen=True)

    username: str
    password: str


def _read_credential(directory: Path) -> _Credential:
    return _Credential(
        username=(directory / "username").read_text(encoding="utf-8").strip(),
        password=(directory / "password").read_text(encoding="utf-8").strip(),
    )


class SecondTransactionError(RuntimeError):
    """A transaction was opened while another of the same request path was open: a second connection, which the
    catalog never takes (ADR 0230 §10). A programming fault, answered 500."""


#: The SQLSTATEs of a server shutting down or not yet accepting connections: an administrator's or a crash's shutdown
#: (57P01, 57P02) and a server starting up or in recovery (57P03).
_SHUTTING_DOWN: Final = frozenset({"57P01", "57P02", "57P03"})


def _unanswered(exc: sa_exc.SQLAlchemyError) -> bool:
    """Whether ``exc`` says the database gave no connection or did not answer, rather than refusing a statement.

    psycopg's ``OperationalError`` family holds refusals too (a password, a serialization failure, a deadlock, a lock
    not available, a limit), each carrying its SQLSTATE; a failure on the client's side of the connection (refused,
    reset, timed out) carries none, and the server reports a lost connection under class 08. A login the server refuses
    carries none either (psycopg 3.3.9 raises it as the connection's failure; measured 2026-11-28 on a role that does
    not exist), so it reads as unanswered.
    """
    if isinstance(exc, sa_exc.TimeoutError):  # the pool gave no connection in time
        return True
    if not isinstance(exc, sa_exc.DBAPIError):
        return False
    if exc.connection_invalidated:
        return True
    if not isinstance(exc, sa_exc.OperationalError | sa_exc.InterfaceError) or not isinstance(exc.orig, psycopg.Error):
        return False
    sqlstate = exc.orig.sqlstate
    return sqlstate is None or sqlstate.startswith("08") or sqlstate in _SHUTTING_DOWN


class Database:
    """The catalog's pool, and the one way to a transaction on it (:func:`open_database`)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def reading(self) -> AsyncGenerator[AsyncSession]:
        """One read-only transaction, committed when the block returns.

        Raises:
            ServiceUnavailableError: No connection was given in time, or the database did not answer.
            SecondTransactionError: A transaction is already open in this context.
        """
        async with self._transaction(_READ_ONLY) as session:
            yield session

    @asynccontextmanager
    async def writing(self) -> AsyncGenerator[AsyncSession]:
        """One read-write transaction, committed when the block returns and rolled back when it raises.

        Raises:
            ServiceUnavailableError: No connection was given in time, or the database did not answer.
            SecondTransactionError: A transaction is already open in this context.
        """
        async with self._transaction(()) as session:
            yield session

    @asynccontextmanager
    async def _transaction(self, first: Sequence[TextClause]) -> AsyncGenerator[AsyncSession]:
        if _OPEN.get():
            raise SecondTransactionError("a transaction is already open on this request path: pass its session down instead of opening another")
        opened = _OPEN.set(True)
        try:
            async with AsyncSession(self._engine, expire_on_commit=False) as session, session.begin():
                connection = await session.connection()
                for statement in first:
                    await connection.execute(statement)
                yield session
        except sa_exc.SQLAlchemyError as exc:
            if _unanswered(exc):
                # A connection lost while committing leaves the commit's outcome unknown (ADR 0230 §6): say only the outage.
                raise ServiceUnavailableError("the catalog's database did not answer") from exc
            raise
        finally:
            _OPEN.reset(opened)

    async def dispose(self) -> None:
        """Close every pooled connection."""
        await self._engine.dispose()


def connector(settings: DatabaseSettings, *, application_name: str = APPLICATION_NAME) -> Callable[[], Awaitable[psycopg.AsyncConnection]]:
    """What opens one connection as the role whose credential ``settings`` names, reading it from its files each time, and
    reporting ``application_name`` to the server."""

    async def connect() -> psycopg.AsyncConnection:
        credential = await run_in_threadpool(_read_credential, settings.database_credentials_dir)
        return await psycopg.AsyncConnection.connect(
            host=settings.database_host,
            port=settings.database_port,
            dbname=settings.database_name,
            user=credential.username,
            password=credential.password or None,
            connect_timeout=settings.database_connect_timeout_seconds,
            application_name=application_name,
            # A time the database stamps is read back in UTC, as every time the catalog answers is.
            options="-c TimeZone=UTC",
        )

    return connect


def open_database(settings: DatabaseSettings) -> Database:
    """The catalog's pool over ``settings``; it connects at the first transaction, never here."""
    engine = create_async_engine(
        "postgresql+psycopg://",
        async_creator=connector(settings),
        pool_size=settings.database_pool_size,
        max_overflow=0,
        pool_timeout=settings.database_acquire_timeout_seconds,
        pool_recycle=settings.database_recycle_seconds,
        pool_pre_ping=True,
    )
    return Database(engine)
