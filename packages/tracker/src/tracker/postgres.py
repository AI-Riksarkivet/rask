"""PostgresTracker — thread-safe, buffered transfer tracker backed by PostgreSQL."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import create_engine

from tracker._base import _BufferedSqlTracker


def _normalize_dsn(dsn: str) -> str:
    """Pin driverless DSNs to the psycopg (3) SQLAlchemy driver."""
    for prefix in ("postgres://", "postgresql://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg://" + dsn.removeprefix(prefix)
    return dsn


class PostgresTracker(_BufferedSqlTracker):
    """Thread-safe, buffered transfer tracker backed by PostgreSQL.

    Same behavior and interface as :class:`~tracker.sqlite.SqliteTracker`,
    but connects to a PostgreSQL server instead of a local file — use it when
    several machines share one tracker, or tracker state must outlive the host.

    Requires the ``postgres`` extra: ``pip install "tracker[postgres]"``.

    Usage::

        with PostgresTracker("postgresql://user:pass@host:5432/rask") as tracker:
            tracker.mark("folder/file.jpg", 12345, TransferStatus.done)

    Pass ``create_schema=False`` when the database's schema is managed elsewhere: a shared server is
    exactly where a client issuing DDL on construction is least welcome.
    """

    def __init__(self, dsn: str, *, flush_every: int = 200, create_schema: bool = True) -> None:
        try:
            engine = create_engine(_normalize_dsn(dsn), echo=False)
        except ModuleNotFoundError as exc:
            raise ImportError('PostgresTracker requires the PostgreSQL driver — install it with: pip install "tracker[postgres]"') from exc
        super().__init__(engine, pg_insert, flush_every=flush_every, create_schema=create_schema)
