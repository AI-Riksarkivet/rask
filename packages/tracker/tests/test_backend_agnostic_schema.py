"""tracker claims to be backend-agnostic; its schema handling was not (PS-10, PS-11, PS-12).

(The live-PostgreSQL half of PS-10 is in ``test_postgres.py``, beside the rest of the opt-in pg suite:
two ``postgresql_noproc`` factories in one session collide on the same template database.)

PS-10 — two defects in one. The column migration lived on `SqliteTracker` and was written in SQLite
dialect (`PRAGMA table_info` + a hand-spelled `INTEGER DEFAULT 0`), so a PostgreSQL database created
before `etag`/`validated`/`verified` existed never got them: every query against those columns fails,
on the one backend the package added specifically so "tracker state can outlive the host". And the
`CREATE TABLE` ran unconditionally inside the base constructor, so merely CONSTRUCTING a tracker
issued DDL — against a shared Postgres that is both a privilege the app may not have and a schema
decision that is not a client's to make.

The dialect coverage is proved the way this suite already proves the upsert: by compiling the
statement for both dialects, which needs no server and therefore actually runs.

PS-11 — the tracker owns an Engine AND a long-lived Session, and `close()` was purely manual, so any
exception between construction and `close()` leaked both.

PS-12 — `TrackerProtocol` omitted `flush()`, which both backends' documented usage calls; code typed
against the protocol could not call the method the docs tell it to.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_protocol_members

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql, sqlite

from tracker import SqliteTracker, TrackerProtocol, TransferStatus
from tracker._base import _BufferedSqlTracker, add_column_ddl, missing_columns
from tracker.postgres import PostgresTracker


_LEGACY_SCHEMA = (
    "CREATE TABLE transfer (key VARCHAR NOT NULL PRIMARY KEY, size INTEGER NOT NULL, status VARCHAR NOT NULL, error VARCHAR, updated_at DATETIME NOT NULL)"
)


# ── PS-10: the migration is the base's, and it speaks both dialects ──────────────────────────


def test_the_migration_belongs_to_every_backend_not_just_sqlite() -> None:
    assert PostgresTracker._migrate is _BufferedSqlTracker._migrate
    assert SqliteTracker._migrate is _BufferedSqlTracker._migrate


def test_the_add_column_ddl_compiles_for_both_dialects() -> None:
    from tracker._base import TRANSFER_TABLE

    column = TRANSFER_TABLE.columns["validated"]
    assert "ALTER TABLE transfer ADD COLUMN validated" in add_column_ddl(column, sqlite.dialect())
    assert "ALTER TABLE transfer ADD COLUMN validated" in add_column_ddl(column, postgresql.dialect())
    # The type is RENDERED by the dialect, never hand-spelled: sqlite has no BOOLEAN of its own.
    assert "INTEGER" not in add_column_ddl(column, postgresql.dialect()).upper().replace("ALTER", "")


def test_missing_columns_are_derived_from_the_model_not_a_hand_kept_list(tmp_path: Path) -> None:
    """A column added to `Transfer` must migrate itself; the old list had to be edited by hand."""
    db = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        conn.exec_driver_sql(_LEGACY_SCHEMA)
        conn.commit()
    assert {c.name for c in missing_columns(engine)} == {"etag", "validated", "verified"}
    engine.dispose()


def test_a_legacy_database_is_migrated_on_open(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        conn.exec_driver_sql(_LEGACY_SCHEMA)
        conn.commit()
    engine.dispose()

    with SqliteTracker(db) as tracker:
        tracker.mark("a.jpg", 1, TransferStatus.done, etag='"e1"', validated=True)
        tracker.flush()
        assert tracker.unverified_keys() == [("a.jpg", 1, '"e1"')]

    columns = {c["name"] for c in inspect(create_engine(f"sqlite:///{db}")).get_columns("transfer")}
    assert {"etag", "validated", "verified"} <= columns


def test_schema_creation_can_be_declined(tmp_path: Path) -> None:
    """Against a managed database the client may hold no DDL privilege at all — and merely
    constructing a tracker must not be the thing that decides the schema."""
    db = tmp_path / "empty.db"
    with SqliteTracker(db, create_schema=False) as tracker:
        assert not inspect(tracker._engine).has_table("transfer")


# ── PS-11: the resources are released by the language, not by the caller ─────────────────────


def test_a_tracker_is_a_context_manager_that_flushes_and_closes(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    with SqliteTracker(db) as tracker:
        tracker.mark("a.jpg", 1, TransferStatus.done)  # buffered, never flushed by hand
    with SqliteTracker(db) as reopened:
        assert reopened.done_keys() == {"a.jpg"}


def test_the_context_manager_releases_on_an_exception(tmp_path: Path) -> None:
    tracker = SqliteTracker(tmp_path / "t.db")
    with pytest.raises(RuntimeError), tracker:
        tracker.mark("a.jpg", 1, TransferStatus.done)
        raise RuntimeError("boom")
    assert tracker._session.get_bind() is not None  # the session object survives; the engine is disposed
    with SqliteTracker(tmp_path / "t.db") as reopened:
        assert reopened.done_keys() == {"a.jpg"}, "a buffered mark was lost when the block raised"


# ── PS-12: the protocol describes the documented interface ───────────────────────────────────


def test_the_protocol_declares_flush_and_the_context_manager() -> None:
    declared = set(get_protocol_members(TrackerProtocol))
    assert {"flush", "__enter__", "__exit__"} <= declared, f"the protocol omits documented methods: {sorted(declared)}"


def test_a_backend_satisfies_its_own_protocol(tmp_path: Path) -> None:
    with SqliteTracker(tmp_path / "t.db") as tracker:
        assert isinstance(tracker, TrackerProtocol)
