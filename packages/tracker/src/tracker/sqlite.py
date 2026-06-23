"""SqliteTracker — thread-safe, buffered transfer tracker backed by SQLite."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import create_engine

from tracker._base import _BufferedSqlTracker


_MIGRATE_COLUMNS = [
    ("etag", "TEXT"),
    ("validated", "INTEGER DEFAULT 0"),
    ("verified", "INTEGER DEFAULT 0"),
]


class SqliteTracker(_BufferedSqlTracker):
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
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            echo=False,
        )
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
            conn.commit()
        super().__init__(engine, sqlite_insert, flush_every=flush_every)
        self._migrate()

    def _migrate(self) -> None:
        """Add new columns to existing databases (backwards-compatible)."""
        with self._engine.connect() as conn:
            existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(transfer)").fetchall()}
            for col_name, col_type in _MIGRATE_COLUMNS:
                if col_name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE transfer ADD COLUMN {col_name} {col_type}")
            conn.commit()
