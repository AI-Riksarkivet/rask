"""The release's migration command, ``python -m catalog.schema migrate``, run twice at once against a fresh database:
both runs succeed and the revisions are applied once (ADR 0230 §11), as two releases' Jobs, or a Job and its retry, may.
Its sessions name themselves to the server apart from the catalog pods'. Against a database a newer release migrated it
refuses, applying nothing, with the line a catalog pod refuses with; a refusal of the database itself (a role it does not
know, a grant a role lacks) ends the command, and a catalog pod's wait, at once on one line.

The command runs as the release runs it, a process of its own as the migrator role. The database is the test's own,
staged fresh through the server's superuser: no ``rask`` schema, owned by the migrator.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import LiteralString

import psycopg
import pytest
from psycopg import sql

from tests.postgres_server import ROLES, PostgresDatabase, PostgresServer, write_credential


def _waiting_on_a_lock(watcher: psycopg.Connection, count: int) -> None:
    """Return once ``count`` of the migration command's sessions wait on a lock, or fail after 60 s."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        row = watcher.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND usename = %s AND application_name = 'rask-catalog-migrate' "
            "AND wait_event_type = 'Lock'",
            (ROLES.migrator,),
        ).fetchone()
        if row is not None and row[0] == count:
            return
        time.sleep(0.05)
    pytest.fail(f"{count} migration runs never waited together")


def _migrator_env(postgres: PostgresServer, catalog_database: PostgresDatabase, tmp_path: Path) -> dict[str, str]:
    """The environment the release gives the migration command, as the migrator role on the test's database."""
    settings = postgres.migrate_settings(catalog_database.name, write_credential(tmp_path / "migrator", ROLES.migrator))
    return {**os.environ, **{name: str(value) for name, value in settings.model_dump(by_alias=True).items()}}


def _stage(catalog_database: PostgresDatabase, staging: LiteralString) -> None:
    """Run ``staging`` on the test's database through the server's superuser, and give the database to the migrator, which
    owns the release's."""
    with psycopg.connect(catalog_database.url, autocommit=True) as superuser:
        superuser.execute(staging)
        superuser.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(sql.Identifier(catalog_database.name), sql.Identifier(ROLES.migrator)))


def test_two_migrations_at_once_leave_the_schema_at_head(postgres: PostgresServer, catalog_database: PostgresDatabase, tmp_path: Path) -> None:
    _stage(catalog_database, "DROP SCHEMA rask CASCADE")
    env = _migrator_env(postgres, catalog_database, tmp_path)
    with psycopg.connect(catalog_database.url) as holder, psycopg.connect(catalog_database.url, autocommit=True) as watcher:
        # The schema's name held uncommitted on a connection of the test's own, so both runs are under way at once
        # before either can create it.
        holder.execute("CREATE SCHEMA rask")
        runs = [
            subprocess.Popen([sys.executable, "-m", "catalog.schema", "migrate"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        _waiting_on_a_lock(watcher, 2)
        holder.rollback()
        answers = [run.communicate(timeout=60) for run in runs]

    assert [(run.returncode, out) for run, (out, _) in zip(runs, answers, strict=True)] == [
        (0, "the catalog's database schema is at revision 0001\n"),
        (0, "the catalog's database schema is at revision 0001\n"),
    ], [err for _, err in answers]


def test_a_migration_against_a_schema_a_newer_release_migrated_refuses_and_applies_nothing(
    postgres: PostgresServer, catalog_database: PostgresDatabase, tmp_path: Path
) -> None:
    _stage(catalog_database, "UPDATE rask.alembic_version SET version_num = '9999'")
    env = _migrator_env(postgres, catalog_database, tmp_path)

    runs = [subprocess.run([sys.executable, "-m", "catalog.schema", "migrate"], env=env, capture_output=True, text=True, timeout=60) for _ in range(2)]

    refused = (
        1,
        "",
        "the catalog's database schema is at revision 9999, which this image does not know (its newest is 0001): a newer release migrated this "
        "database; run a release at least as new\n",
    )
    assert [(run.returncode, run.stdout, run.stderr) for run in runs] == [refused, refused]


@pytest.mark.parametrize(
    ("command", "staging", "said"),
    [
        pytest.param(
            "migrate",
            "DROP SCHEMA rask CASCADE",
            'the catalog\'s database refused: role "rask_absent" does not exist\n',
            id="a-revision-granting-to-a-role-the-database-does-not-know",
        ),
        pytest.param(
            "wait",
            "REVOKE SELECT ON rask.alembic_version FROM rask_catalog",
            "the catalog's database refused: permission denied for table alembic_version\n",
            id="a-pod-whose-role-may-not-read-the-schemas-revision",
        ),
    ],
)
def test_a_refusal_of_the_database_ends_the_command_at_once_on_one_line(
    postgres: PostgresServer, catalog_database: PostgresDatabase, tmp_path: Path, command: str, staging: LiteralString, said: str
) -> None:
    """A role name the platform got wrong, or a grant a role lacks, is no outage: the command says the database's own
    words on one line and exits, rather than waiting out its deadline or printing a traceback."""
    _stage(catalog_database, staging)
    env = {**_migrator_env(postgres, catalog_database, tmp_path), "RASK_DATABASE_CATALOG_ROLE": "rask_absent"} if command == "migrate" else dict(os.environ)

    run = subprocess.run([sys.executable, "-m", "catalog.schema", command], env=env, capture_output=True, text=True, timeout=60)

    assert (run.returncode, run.stdout, run.stderr) == (1, "", said)
