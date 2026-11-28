"""The catalog's database: one async engine a process, and one transaction a request path (ADR 0230 §8, §10).

The engine is private to this module. A door reaches the database only through :meth:`Database.reading` and
:meth:`Database.writing`, each one transaction on one pooled connection, and passes the session it yields down to every
query of its path, so no path holds two connections at once: a second transaction opened while one is open in the same
context is refused (:class:`SecondTransactionError`). A door leaves its block, and so commits, before it announces and
before it answers, which is why the session is no FastAPI dependency (a ``yield`` dependency would commit after the
response is sent).

What the database answers leaves this module as the spec's errors (:func:`_answered`). A write's commit is a step of
its own, after what the block sent is flushed. Before it, nothing applied: a database that did not answer is the outage a
retry may heal (503), and one that refused is an internal error (500). A commit is proven rolled back only by the
server's answer; without it the write may have applied, which is an error of its own and never a retryable one
(:class:`~lakehouse.problems.ns_errors.CommitOutcomeUnknownError`, ADR 0230 §6). A write reports its commit to its
request just before it begins (:func:`~lakehouse.process.request_commits.commit_begins`): a request deadline passing from
then on answers the same, and a request its deadline already answered 503 refuses the commit, so the write rolls back.

Each NEW connection reads the role's ``username`` and ``password`` from the credential directory the secret store
renders, so a credential replaced there is the next connection's, with no restart; a connection is recycled within the
credential's life (``pool_recycle``).
"""

from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Final

import psycopg
from fastapi.concurrency import run_in_threadpool
from lance_namespace import InternalError, LanceNamespaceError, ServiceUnavailableError
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import TextClause, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from lakehouse.problems.ns_errors import CommitOutcomeUnknownError
from lakehouse.process.request_commits import commit_begins


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


_NO_ANSWER: Final = "the catalog's database did not answer"

#: The one session end that proves a commit rolled back: idle in its transaction, the session ended before the commit
#: reached it (measured 2026-11-28 on psycopg 3.3.9: FATAL 25P03 at the commit and at the next statement alike).
_IDLE_IN_TRANSACTION_ENDED: Final = "25P03"
#: The SQLSTATEs of a session the server ended or would not begin, which leaves nothing uncommitted applied: an
#: administrator's or a crash's shutdown (57P01, 57P02), a server starting up or in recovery (57P03), and a session ended
#: for idling in its transaction.
_SESSION_ENDED: Final = frozenset({"57P01", "57P02", "57P03", _IDLE_IN_TRANSACTION_ENDED})
#: The SQLSTATE classes that leave a failed commit's outcome unknown even when the server answered: a connection
#: exception (08), insufficient resources (53), operator intervention (57: a terminated session, a shutdown, a cancel),
#: a system error (58) and an internal error (XX).
_COMMIT_UNKNOWN_CLASSES: Final = ("08", "53", "57", "58", "XX")
#: The server's own "statement completion unknown".
_COMPLETION_UNKNOWN: Final = "40003"


def _answered(exc: sa_exc.SQLAlchemyError, *, committing: bool) -> LanceNamespaceError | None:
    """The spec's error for what the database answered when ``exc`` was raised, ``committing`` a write's commit or not;
    ``None`` for a failure that is no answer of the database's, which is raised as it is: SQLAlchemy refusing how the session
    was used (one used after its transaction failed), or the client's own fault with no SQLSTATE.

    Every statement the server refuses is a refusal, whatever the reason: a constraint the write broke, a lock it could
    not take, and the code's own mistake the server names (a syntax error, a missing GRANT) alike. The answer is the 500 a
    fault gets, and the server's words and SQLSTATE stay in the log through the cause. A door that maps a refusal of its
    own (a constraint it broke) catches the statement's error inside its block, before the block's end answers it.

    psycopg's ``OperationalError`` family holds refusals too (a password, a serialization failure, a deadlock, a lock not
    available, a limit), each carrying its SQLSTATE. A failure on the client's side of the connection (refused, reset,
    timed out) carries none, and the server reports a lost connection under class 08. A login the server refuses carries
    none either (psycopg 3.3.9 raises it as the connection's failure; measured 2026-11-28 on a role that does not exist),
    so it reads as unanswered. A write's commit is proven rolled back only by an ERROR outside 40003 and the classes of
    :data:`_COMMIT_UNKNOWN_CLASSES`, or by a session ended idle in its transaction (FATAL 25P03).
    """
    if isinstance(exc, sa_exc.TimeoutError):  # the pool gave no connection in time
        return ServiceUnavailableError(_NO_ANSWER)
    if not isinstance(exc, sa_exc.DBAPIError) or not isinstance(exc.orig, psycopg.Error):
        return None
    sqlstate, severity = exc.orig.sqlstate, exc.orig.diag.severity_nonlocalized
    if committing:
        if severity == "FATAL" and sqlstate == _IDLE_IN_TRANSACTION_ENDED:
            return ServiceUnavailableError(_NO_ANSWER)
        if severity == "ERROR" and sqlstate is not None and sqlstate != _COMPLETION_UNKNOWN and not sqlstate.startswith(_COMMIT_UNKNOWN_CLASSES):
            return _refusal(exc.orig)
        return CommitOutcomeUnknownError("the catalog's database did not confirm the commit, which may have applied")
    if exc.connection_invalidated:
        return ServiceUnavailableError(_NO_ANSWER)
    if sqlstate is None:
        # The server sent nothing: a connection's failure is the outage, and anything else the client's own fault.
        return ServiceUnavailableError(_NO_ANSWER) if isinstance(exc, sa_exc.OperationalError | sa_exc.InterfaceError) else None
    if sqlstate.startswith("08") or sqlstate in _SESSION_ENDED:
        return ServiceUnavailableError(_NO_ANSWER)
    return _refusal(exc.orig)


def _refusal(answer: psycopg.Error) -> InternalError:
    """The database's refusal in its own words, on one line, as a pod's wait and the schema command print it."""
    return InternalError(f"the catalog's database refused: {' '.join(str(answer).split())}")


@contextmanager
def _answered_as_the_spec(*, committing: bool = False) -> Generator[None]:
    """Raise a failure of the database's as the spec's error for what it answered (:func:`_answered`)."""
    try:
        yield
    except sa_exc.SQLAlchemyError as exc:
        if (answered := _answered(exc, committing=committing)) is None:
            raise
        raise answered from exc


async def _begin(session: AsyncSession, first: Sequence[TextClause] = ()) -> None:
    """Take the session's connection, which begins its transaction, and run ``first`` in it."""
    connection = await session.connection()
    for statement in first:
        await connection.execute(statement)


class Database:
    """The catalog's pool, and the one way to a transaction on it (:func:`open_database`)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def reading(self) -> AsyncGenerator[AsyncSession]:
        """One read-only transaction, committed when the block returns; it applies nothing, so a commit the database did
        not answer is only the outage.

        Raises:
            ServiceUnavailableError: No connection was given in time, or the database did not answer.
            InternalError: The database refused a statement of the block.
            SecondTransactionError: A transaction is already open in this context.
        """
        async with self._session() as session:
            with _answered_as_the_spec():
                await _begin(session, _READ_ONLY)
                yield session
                await session.commit()

    @asynccontextmanager
    async def writing(self) -> AsyncGenerator[AsyncSession]:
        """One read-write transaction, committed when the block returns and rolled back when it raises.

        Raises:
            ServiceUnavailableError: No connection was given in time, or the database did not answer before the commit;
                nothing applied.
            InternalError: The database refused a statement of the block or the commit; nothing applied.
            CommitOutcomeUnknownError: The commit got no answer that proves it rolled back, so the write may have applied.
            LateCommitRefusedError: The request was answered at its deadline before the commit began; nothing applied.
            SecondTransactionError: A transaction is already open in this context.
        """
        async with self._session() as session:
            with _answered_as_the_spec():
                await _begin(session)
                yield session
                await session.flush()
            commit_begins()
            with _answered_as_the_spec(committing=True):
                await session.commit()

    @asynccontextmanager
    async def _session(self) -> AsyncGenerator[AsyncSession]:
        """A session of this context's one transaction; it rolls back whatever it did not commit when it closes."""
        if _OPEN.get():
            raise SecondTransactionError("a transaction is already open on this request path: pass its session down instead of opening another")
        opened = _OPEN.set(True)
        try:
            async with AsyncSession(self._engine, expire_on_commit=False) as session:
                yield session
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
