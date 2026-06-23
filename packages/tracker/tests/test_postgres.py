"""Tests for the PostgreSQL tracker backend and backend selection.

The integration tests run against a real PostgreSQL server via
``pytest-postgresql`` noproc fixtures and are opt-in — they skip unless a
server is pointed at explicitly, so a default test run never touches
whatever happens to listen on the local default port::

    docker run --rm -e POSTGRES_HOST_AUTH_METHOD=trust -p 5433:5432 postgres:16
    uv run pytest --postgresql-port=5433 --postgresql-user=postgres
"""

from pathlib import Path

import pytest
from pytest_postgresql import factories
from sqlalchemy.dialects import postgresql, sqlite

from tracker import (
    PostgresTracker,
    SqliteTracker,
    TransferStatus,
    create_tracker,
)
from tracker._base import _upsert_statement
from tracker.postgres import _normalize_dsn


# ── DSN normalization ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "dsn,expected",
    [
        ("postgres://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("postgresql://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        ("postgresql+psycopg://u@host/db", "postgresql+psycopg://u@host/db"),
        ("postgresql+psycopg2://u@host/db", "postgresql+psycopg2://u@host/db"),
    ],
)
def test_normalize_dsn(dsn, expected):
    assert _normalize_dsn(dsn) == expected


# ── Backend selection ───────────────────────────────────────────────


def test_create_tracker_path_selects_sqlite(tmp_path: Path):
    tracker = create_tracker(tmp_path / "t.db")
    assert isinstance(tracker, SqliteTracker)
    tracker.close()


def test_create_tracker_path_string_selects_sqlite(tmp_path: Path):
    tracker = create_tracker(str(tmp_path / "t.db"))
    assert isinstance(tracker, SqliteTracker)
    tracker.close()


def test_create_tracker_postgres_dsn_selects_postgres(monkeypatch):
    created = {}

    class FakePostgresTracker:
        def __init__(self, dsn, *, flush_every):
            created["dsn"] = dsn
            created["flush_every"] = flush_every

    monkeypatch.setattr("tracker.factory.PostgresTracker", FakePostgresTracker)
    tracker = create_tracker("postgresql://u:p@host/db", flush_every=42)
    assert isinstance(tracker, FakePostgresTracker)
    assert created == {"dsn": "postgresql://u:p@host/db", "flush_every": 42}


def test_create_tracker_rejects_unknown_scheme():
    with pytest.raises(ValueError, match="Unsupported tracker DSN scheme 'mysql'"):
        create_tracker("mysql://u:p@host/db")


def test_create_tracker_rejects_unknown_scheme_wrapped_in_path():
    with pytest.raises(ValueError, match="Unsupported tracker DSN scheme"):
        create_tracker(Path("mysql://u:p@host/db"))


def test_create_tracker_postgres_dsn_wrapped_in_path_selects_postgres(monkeypatch):
    seen = {}

    class FakePostgresTracker:
        def __init__(self, dsn, *, flush_every):
            seen["dsn"] = dsn
            seen["flush_every"] = flush_every

    monkeypatch.setattr("tracker.factory.PostgresTracker", FakePostgresTracker)
    tracker = create_tracker(Path("postgresql://u@host/db"))
    assert isinstance(tracker, FakePostgresTracker)
    assert seen["dsn"] == "postgresql://u@host/db"


def test_create_tracker_repairs_single_slash_dsn(monkeypatch):
    seen = {}

    class FakePostgresTracker:
        def __init__(self, dsn, *, flush_every):
            seen["dsn"] = dsn
            seen["flush_every"] = flush_every

    monkeypatch.setattr("tracker.factory.PostgresTracker", FakePostgresTracker)
    create_tracker("postgresql:/user:pass@host/db")
    assert seen["dsn"] == "postgresql://user:pass@host/db"


def test_create_tracker_windows_drive_path_is_sqlite(tmp_path: Path):
    # Single-letter "schemes" (Windows drive letters) must stay file paths.
    tracker = create_tracker(str(tmp_path / "t.db"))
    assert isinstance(tracker, SqliteTracker)
    tracker.close()


def test_postgres_tracker_missing_driver_raises_actionable_error(monkeypatch):
    def boom(*args: object, **kwargs: object) -> None:
        raise ModuleNotFoundError("No module named 'psycopg'")

    monkeypatch.setattr("tracker.postgres.create_engine", boom)
    with pytest.raises(ImportError, match=r"tracker\[postgres\]"):
        PostgresTracker("postgresql://u@host/db")


def test_redact_dsn_masks_password():
    from tracker import redact_dsn

    redacted = redact_dsn("postgresql://user:s3cret@db-host:5432/rask")
    assert "s3cret" not in redacted
    assert "user" in redacted and "db-host" in redacted


def test_redact_dsn_without_password_is_unchanged():
    from tracker import redact_dsn

    assert redact_dsn("postgresql://user@db-host/rask") == "postgresql://user@db-host/rask"


# ── Upsert statement (compiled per dialect, no server needed) ──────


ROWS = [
    {
        "key": "a.jpg",
        "size": 1,
        "status": TransferStatus.done,
        "error": None,
        "etag": None,
        "validated": False,
        "verified": False,
        "updated_at": None,
    }
]


def test_upsert_statement_compiles_for_postgres():
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    sql = str(_upsert_statement(pg_insert, ROWS).compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in sql and "DO UPDATE SET" in sql
    assert "excluded.status" in sql


def test_upsert_statement_compiles_for_sqlite():
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    sql = str(_upsert_statement(sqlite_insert, ROWS).compile(dialect=sqlite.dialect()))
    assert "ON CONFLICT" in sql and "DO UPDATE SET" in sql
    assert "excluded.status" in sql


# ── Integration: real PostgreSQL via pytest-postgresql ─────────────


@pytest.fixture(scope="session")
def _pg_server(request: pytest.FixtureRequest) -> None:
    """Opt-in gate: integration tests need an explicitly configured server."""
    if not request.config.getoption("--postgresql-port"):
        pytest.skip("PostgreSQL integration tests are opt-in — pass --postgresql-port (see module docstring)")


postgresql_server = factories.postgresql_noproc()
postgresql_conn = factories.postgresql("postgresql_server")


@pytest.fixture
def pg_tracker(_pg_server, postgresql_conn):
    """A PostgresTracker connected to the per-test database."""
    info = postgresql_conn.info
    password = f":{info.password}" if info.password else ""
    dsn = f"postgresql://{info.user}{password}@{info.host}:{info.port}/{info.dbname}"
    tracker = PostgresTracker(dsn, flush_every=500)
    yield tracker
    tracker.close()


def test_postgres_mark_flush_and_query(pg_tracker):
    pg_tracker.mark("a.jpg", 100, TransferStatus.done, etag='"e1"', validated=True)
    pg_tracker.mark("b.jpg", 0, TransferStatus.error, error="download: HTTP 500")
    pg_tracker.flush()

    assert pg_tracker.done_keys() == {"a.jpg"}
    assert pg_tracker.error_entries() == [("b.jpg", 0)]
    assert pg_tracker.error_details() == [("b.jpg", 0, "download: HTTP 500")]
    assert pg_tracker.summary() == {"pending": 0, "done": 1, "error": 1}


def test_postgres_same_key_twice_in_one_buffer_last_wins(pg_tracker):
    # Duplicate keys in one flush would raise "cannot affect row a second
    # time" on Postgres without the buffer dedupe.
    pg_tracker.mark("a.jpg", 0, TransferStatus.error, error="download: timeout")
    pg_tracker.mark("a.jpg", 100, TransferStatus.done)
    pg_tracker.flush()

    assert pg_tracker.done_keys() == {"a.jpg"}
    assert pg_tracker.error_entries() == []


def test_postgres_remark_updates_existing_row(pg_tracker):
    pg_tracker.mark("a.jpg", 100, TransferStatus.error, error="first failure")
    pg_tracker.flush()
    pg_tracker.mark("a.jpg", 100, TransferStatus.done)
    pg_tracker.flush()

    assert pg_tracker.error_entries() == []
    assert pg_tracker.done_keys() == {"a.jpg"}


def test_postgres_delete_removes_entry(pg_tracker):
    pg_tracker.mark("ok.jpg", 100, TransferStatus.done)
    pg_tracker.mark("bad/__manifest__", 0, TransferStatus.error, error="manifest: 403")

    pg_tracker.delete("bad/__manifest__")

    assert pg_tracker.error_details() == []
    assert pg_tracker.summary() == {"pending": 0, "done": 1, "error": 0}


def test_postgres_unverified_and_unvalidated(pg_tracker):
    pg_tracker.mark("a.jpg", 1, TransferStatus.done, validated=True, verified=True)
    pg_tracker.mark("b.jpg", 2, TransferStatus.done, etag='"e2"')

    assert pg_tracker.unverified_keys() == [("b.jpg", 2, '"e2"')]
    assert pg_tracker.unvalidated_keys() == [("b.jpg", 2)]


def test_postgres_via_factory_end_to_end(_pg_server, postgresql_conn):
    info = postgresql_conn.info
    dsn = f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"
    tracker = create_tracker(dsn)
    assert isinstance(tracker, PostgresTracker)
    tracker.mark("x.jpg", 1, TransferStatus.done)
    tracker.commit()
    assert tracker.done_keys() == {"x.jpg"}
    tracker.close()
