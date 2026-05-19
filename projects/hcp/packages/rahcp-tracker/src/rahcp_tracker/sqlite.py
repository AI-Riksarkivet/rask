"""SqliteTracker — thread-safe, buffered transfer tracker backed by SQLite."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, func, select, text

from rahcp_tracker.models import Transfer, TransferStatus

_UPSERT_SQL = text(
    "INSERT INTO transfer (key, size, status, error, etag, validated, verified, updated_at) "
    "VALUES (:key, :size, :status, :error, :etag, :validated, :verified, :updated_at) "
    "ON CONFLICT(key) DO UPDATE SET "
    "status=:status, size=:size, error=:error, etag=:etag, "
    "validated=:validated, verified=:verified, updated_at=:updated_at"
)

_MIGRATE_COLUMNS = [
    ("etag", "TEXT"),
    ("validated", "INTEGER DEFAULT 0"),
    ("verified", "INTEGER DEFAULT 0"),
]


class SqliteTracker:
    """Thread-safe, buffered transfer tracker backed by SQLite.

    Marks are buffered in memory and flushed to the database either
    explicitly via ``flush()`` or automatically when the buffer reaches
    ``flush_every`` entries.

    Usage::

        tracker = SqliteTracker(Path(".upload-tracker.db"))
        tracker.mark("folder/file.jpg", 12345, TransferStatus.done, etag='"abc123"')
        tracker.flush()
        done = tracker.done_keys()
        tracker.close()
    """

    def __init__(self, db_path: Path, *, flush_every: int = 200) -> None:
        self._engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            echo=False,
        )
        with self._engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
            conn.commit()

        SQLModel.metadata.create_all(self._engine)
        self._migrate()
        self._session = Session(self._engine)
        self._lock = threading.Lock()
        self._buffer: list[dict] = []
        self._flush_every = flush_every

    def _migrate(self) -> None:
        """Add new columns to existing databases (backwards-compatible)."""
        with self._engine.connect() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(transfer)"
                ).fetchall()
            }
            for col_name, col_type in _MIGRATE_COLUMNS:
                if col_name not in existing:
                    conn.exec_driver_sql(
                        f"ALTER TABLE transfer ADD COLUMN {col_name} {col_type}"
                    )
            conn.commit()

    def done_keys(self) -> set[str]:
        """Return all keys with status 'done'."""
        self.flush()
        rows = self._session.exec(
            select(Transfer.key).where(Transfer.status == TransferStatus.done)
        ).all()
        return set(rows)

    def error_entries(self) -> list[tuple[str, int]]:
        """Return (key, size) pairs for all failed transfers."""
        self.flush()
        rows = self._session.exec(
            select(Transfer).where(Transfer.status == TransferStatus.error)
        ).all()
        return [(r.key, r.size) for r in rows]

    def mark(
        self,
        key: str,
        size: int,
        status: TransferStatus,
        error: str = "",
        *,
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
        now = datetime.now(timezone.utc)
        conn = self._session.connection()
        for entry in self._buffer:
            conn.execute(
                _UPSERT_SQL,
                {
                    "key": entry["key"],
                    "size": entry["size"],
                    "status": entry["status"].value,
                    "error": entry["error"],
                    "etag": entry["etag"],
                    "validated": 1 if entry["validated"] else 0,
                    "verified": 1 if entry["verified"] else 0,
                    "updated_at": now.isoformat(),
                },
            )
        self._session.commit()
        self._buffer.clear()

    def commit(self) -> None:
        """Flush buffer and commit."""
        self.flush()

    def close(self) -> None:
        """Flush remaining buffer and release resources."""
        self.flush()
        self._session.close()
        self._engine.dispose()

    def summary(self) -> dict[str, int]:
        """Return counts grouped by status."""
        self.flush()
        return {
            status.value: self._session.exec(
                select(func.count()).where(Transfer.status == status)
            ).one()
            for status in TransferStatus
        }

    def unverified_keys(self) -> list[tuple[str, int, str | None]]:
        """Return (key, size, etag) for done files not yet verified."""
        self.flush()
        rows = self._session.exec(
            select(Transfer).where(
                Transfer.status == TransferStatus.done,
                Transfer.verified == False,  # noqa: E712
            )
        ).all()
        return [(r.key, r.size, r.etag) for r in rows]

    def unvalidated_keys(self) -> list[tuple[str, int]]:
        """Return (key, size) for done files not yet validated."""
        self.flush()
        rows = self._session.exec(
            select(Transfer).where(
                Transfer.status == TransferStatus.done,
                Transfer.validated == False,  # noqa: E712
            )
        ).all()
        return [(r.key, r.size) for r in rows]
