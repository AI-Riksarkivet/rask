"""Shared buffered-tracker implementation for SQL backends.

Backends differ only in how the engine is built and which dialect ``insert``
construct provides the upsert; everything else — buffering, flushing, and the
``TrackerProtocol`` queries — is identical and lives here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import Insert as PostgresInsert
from sqlalchemy.dialects.sqlite import Insert as SqliteInsert
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

    Marks are buffered in memory and flushed to the database either
    explicitly via ``flush()`` or automatically when the buffer reaches
    ``flush_every`` entries.
    """

    def __init__(
        self,
        engine: Engine,
        insert_fn: Callable[[type[Transfer]], _DialectInsert],
        *,
        flush_every: int = 200,
    ) -> None:
        self._engine = engine
        self._insert = insert_fn
        SQLModel.metadata.create_all(self._engine)
        self._session = Session(self._engine)
        self._lock = threading.Lock()
        self._buffer: list[dict[str, Any]] = []
        self._flush_every = flush_every

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
