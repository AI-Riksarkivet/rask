"""Shared buffered-tracker implementation for SQL backends.

Backends differ only in how the engine is built and which dialect ``insert``
construct provides the upsert; everything else — buffering, flushing, schema care, and the
``TrackerProtocol`` queries — is identical and lives here.

SCHEMA CARE IS BACKEND-AGNOSTIC, and it was not. The additive column migration lived on
``SqliteTracker`` and was written in SQLite dialect (``PRAGMA table_info`` plus a hand-spelled
``INTEGER DEFAULT 0``), against a hand-kept list of column names — so a PostgreSQL database created
before ``etag``/``validated``/``verified`` existed never got them, on exactly the backend that was
added so tracker state could outlive one host. It is now derived from the model and rendered by the
engine's own dialect, so a new field on :class:`~tracker.models.Transfer` migrates itself on both.

DDL is also no longer unconditional. Constructing a tracker used to issue ``CREATE TABLE`` as a side
effect; against a managed database that is both a privilege the client may not hold and a decision
that is not a client's to make. ``create_schema=False`` opts out — see :class:`_BufferedSqlTracker`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Self

from sqlalchemy import Column, Engine, Table, inspect, literal
from sqlalchemy.dialects.postgresql import Insert as PostgresInsert
from sqlalchemy.dialects.sqlite import Insert as SqliteInsert
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql.schema import ScalarElementColumnDefault
from sqlmodel import Session, SQLModel, col, func, select

from tracker.models import Transfer, TransferStatus


type _DialectInsert = SqliteInsert | PostgresInsert

_UPDATE_COLUMNS = (
    "size",
    "status",
    "error",
    "etag",
    "validated",
    "verified",
    "updated_at",
)

# Rows per statement, well under the bind-parameter limits at 8 params/row
# (SQLite: 32766 variables, Postgres: 65535).
_MAX_ROWS_PER_STMT = 1000

#: The `transfer` table, taken from the shared metadata rather than ``Transfer.__table__``: SQLModel
#: injects the declarative attributes at class-creation time, where a type checker cannot see them.
TRANSFER_TABLE: Table = SQLModel.metadata.tables[Transfer.__name__.lower()]


def missing_columns(engine: Engine) -> list[Column]:
    """Columns :class:`~tracker.models.Transfer` declares that the live table does not have.

    Derived from the model rather than from a hand-kept list, so adding a field is one edit instead
    of two — the old list was already the only thing standing between a new column and a database
    that silently lacks it.
    """
    inspector = inspect(engine)
    if not inspector.has_table(TRANSFER_TABLE.name):
        return []
    present = {column["name"] for column in inspector.get_columns(TRANSFER_TABLE.name)}
    return [column for column in TRANSFER_TABLE.columns if column.name not in present]


def add_column_ddl(column: Column, dialect: Dialect) -> str:
    """The ``ALTER TABLE … ADD COLUMN`` for one column, TYPE and DEFAULT rendered by ``dialect``.

    Never ``NOT NULL``: an added column applies to rows that already exist, and a default is what
    keeps them queryable (a `validated` left NULL matches neither ``== True`` nor ``== False``).
    Never hand-spelled either — ``BOOLEAN`` and its literal differ per backend (`0` vs `false`), and
    hand-spelling them is precisely how the migration became SQLite-only.
    """
    clause = f"ALTER TABLE {TRANSFER_TABLE.name} ADD COLUMN {column.name} {column.type.compile(dialect)}"
    default = column.default
    if isinstance(default, ScalarElementColumnDefault):
        rendered = literal(default.arg, column.type).compile(dialect=dialect, compile_kwargs={"literal_binds": True})
        clause = f"{clause} DEFAULT {rendered}"
    return clause


def _upsert_statement(insert_fn: Callable[[type[Transfer]], _DialectInsert], rows: list[dict[str, Any]]) -> _DialectInsert:
    """Build a multi-row "insert or update on key conflict" statement.

    ``insert_fn`` is a dialect ``insert`` (sqlite or postgresql); both expose
    the same ``on_conflict_do_update`` API, so the statement shape is shared.
    """
    stmt = insert_fn(Transfer).values(rows)
    return stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={col: getattr(stmt.excluded, col) for col in _UPDATE_COLUMNS},
    )


class _BufferedSqlTracker:
    """Thread-safe, buffered transfer tracker over a SQLAlchemy engine.

    Marks are buffered in memory and flushed to the database either explicitly via ``flush()``,
    automatically when the buffer reaches ``flush_every`` entries, or on leaving the ``with`` block.

    A CONTEXT MANAGER, because it owns two things the garbage collector will not release in time —
    a long-lived ``Session`` and the Engine's connection pool — and the buffer, whose contents are
    lost unless something flushes it.

    ``create_schema=False`` skips both the ``CREATE TABLE`` and the additive column migration, for a
    database whose schema someone else owns (and whose role may hold no DDL privilege at all).
    """

    def __init__(
        self,
        engine: Engine,
        insert_fn: Callable[[type[Transfer]], _DialectInsert],
        *,
        flush_every: int = 200,
        create_schema: bool = True,
    ) -> None:
        self._engine = engine
        self._insert = insert_fn
        if create_schema:
            SQLModel.metadata.create_all(self._engine)
            self._migrate()
        self._session = Session(self._engine)
        self._lock = threading.Lock()
        self._buffer: list[dict[str, Any]] = []
        self._flush_every = flush_every

    def _migrate(self) -> None:
        """Add the columns the model declares and the live table lacks, in the engine's dialect."""
        missing = missing_columns(self._engine)
        if not missing:
            return
        with self._engine.connect() as conn:
            for column in missing:
                conn.exec_driver_sql(add_column_ddl(column, self._engine.dialect))
            conn.commit()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Flush and release — ON THE ERROR PATH TOO.

        The buffer holds work that already HAPPENED: files transferred, files that failed with a
        reason. Discarding it because something else raised is how a resumable run redoes work it
        finished, and how the failure reasons vanish exactly when someone needs them.
        """
        self.close()

    def done_keys(self) -> set[str]:
        """Return all keys with status 'done'."""
        with self._lock:
            self._flush_locked()
            rows = self._session.exec(select(col(Transfer.key)).where(Transfer.status == TransferStatus.done)).all()
            return set(rows)

    def error_entries(self) -> list[tuple[str, int]]:
        """Return (key, size) pairs for all failed transfers."""
        with self._lock:
            self._flush_locked()
            rows = self._session.exec(select(Transfer).where(Transfer.status == TransferStatus.error)).all()
            return [(r.key, r.size) for r in rows]

    def error_details(self) -> list[tuple[str, int, str | None]]:
        """Return (key, size, reason) for all failed transfers.

        Like :meth:`error_entries` but also surfaces the recorded failure reason
        (phase-prefixed, e.g. ``"download: …"`` / ``"validate: …"``).
        """
        with self._lock:
            self._flush_locked()
            rows = self._session.exec(select(Transfer).where(Transfer.status == TransferStatus.error)).all()
            return [(r.key, r.size, r.error) for r in rows]

    def delete(self, key: str) -> None:
        """Flush the buffer, then delete the entry for ``key`` if present."""
        with self._lock:
            self._flush_locked()
            row = self._session.exec(select(Transfer).where(Transfer.key == key)).first()
            if row is not None:
                self._session.delete(row)
                self._session.commit()

    def mark(
        self,
        key: str,
        size: int,
        status: TransferStatus,
        *,
        error: str = "",
        etag: str | None = None,
        validated: bool = False,
        verified: bool = False,
    ) -> None:
        """Buffer a status update. Auto-flushes when buffer is full."""
        with self._lock:
            self._buffer.append(
                {
                    "key": key,
                    "size": size,
                    "status": status,
                    "error": error or None,
                    "etag": etag,
                    "validated": validated,
                    "verified": verified,
                }
            )
            if len(self._buffer) >= self._flush_every:
                self._flush_locked()

    def flush(self) -> None:
        """Write all buffered marks to the database."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        now = datetime.now(UTC)
        # Last mark per key wins — and a multi-row upsert must not contain
        # the same key twice ("cannot affect row a second time" on Postgres).
        latest = {entry["key"]: entry for entry in self._buffer}
        rows = [{**entry, "updated_at": now} for entry in latest.values()]
        conn = self._session.connection()
        for i in range(0, len(rows), _MAX_ROWS_PER_STMT):
            conn.execute(_upsert_statement(self._insert, rows[i : i + _MAX_ROWS_PER_STMT]))
        self._session.commit()
        self._buffer.clear()

    def commit(self) -> None:
        """Flush buffer and commit."""
        self.flush()

    def close(self) -> None:
        """Flush remaining buffer and release resources."""
        with self._lock:
            self._flush_locked()
            self._session.close()
        self._engine.dispose()

    def summary(self) -> dict[str, int]:
        """Return counts grouped by status."""
        with self._lock:
            self._flush_locked()
            return {status.value: self._session.exec(select(func.count()).where(Transfer.status == status)).one() for status in TransferStatus}

    def unverified_keys(self) -> list[tuple[str, int, str | None]]:
        """Return (key, size, etag) for done files not yet verified."""
        with self._lock:
            self._flush_locked()
            rows = self._session.exec(
                select(Transfer).where(
                    Transfer.status == TransferStatus.done,
                    Transfer.verified == False,  # noqa: E712
                )
            ).all()
            return [(r.key, r.size, r.etag) for r in rows]

    def unvalidated_keys(self) -> list[tuple[str, int]]:
        """Return (key, size) for done files not yet validated."""
        with self._lock:
            self._flush_locked()
            rows = self._session.exec(
                select(Transfer).where(
                    Transfer.status == TransferStatus.done,
                    Transfer.validated == False,  # noqa: E712
                )
            ).all()
            return [(r.key, r.size) for r in rows]
