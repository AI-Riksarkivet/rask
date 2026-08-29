"""SqliteTracker — thread-safe, buffered transfer tracker backed by SQLite."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import create_engine

from tracker._base import _BufferedSqlTracker


class SqliteTracker(_BufferedSqlTracker):
    """Thread-safe, buffered transfer tracker backed by SQLite.

    Marks are buffered in memory and flushed to the database either explicitly via ``flush()``,
    automatically when the buffer reaches ``flush_every`` entries, or on leaving the ``with`` block.

    Usage::

        with SqliteTracker(Path(".upload-tracker.db")) as tracker:
            tracker.mark("folder/file.jpg", 12345, TransferStatus.done, etag='"abc123"')
            done = tracker.done_keys()

    The additive column migration for an older database file is the base class's and speaks the
    engine's dialect; ``create_schema=False`` declines both it and the ``CREATE TABLE``.
    """

    def __init__(self, db_path: Path, *, flush_every: int = 200, create_schema: bool = True) -> None:
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            echo=False,
        )
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
            conn.commit()
        super().__init__(engine, sqlite_insert, flush_every=flush_every, create_schema=create_schema)
