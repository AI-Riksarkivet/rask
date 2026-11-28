"""The suite's Postgres fixture: a real server per worker, and a database per test that no other test sees."""

import pathlib
import subprocess
import sys

import psycopg
import pytest

from tests.postgres_server import PostgresDatabase, PostgresInstallMissingError, PostgresServer


def test_a_test_reads_back_only_what_it_wrote_to_its_database(postgres: PostgresServer, postgres_database: PostgresDatabase) -> None:
    # The next test's database comes from the same server and the same call the fixture makes, so a database shared
    # between tests shows here whatever worker or distribution mode runs this test.
    with psycopg.connect(postgres_database.url, autocommit=True) as connection:
        connection.execute("CREATE TABLE marks (writer text NOT NULL)")
        connection.execute("INSERT INTO marks (writer) VALUES ('this test')")
        written = connection.execute("SELECT current_database(), writer FROM marks").fetchall()
    following = postgres.create_database()
    try:
        with psycopg.connect(following.url, autocommit=True) as connection:
            seen = connection.execute("SELECT current_database(), to_regclass('marks') IS NOT NULL").fetchall()
    finally:
        postgres.drop_database(following)

    assert (written, seen) == ([(postgres_database.name, "this test")], [(following.name, False)])


_INNER_TESTS = """
import psycopg

names = {}


def test_a_failing_test(postgres_database):
    names["failed"] = postgres_database.name
    assert False


def test_a_passing_test(postgres_database):
    names["passed"] = postgres_database.name


def test_what_the_two_left(postgres_database):
    with psycopg.connect(postgres_database.url) as connection:
        held = {name for (name,) in connection.execute("SELECT datname FROM pg_database").fetchall()}
    assert (names["failed"] in held, names["passed"] in held) == (True, False)
"""


def test_a_failing_tests_database_is_kept_and_named_and_a_passing_ones_dropped(pytester: pytest.Pytester) -> None:
    pytester.makeconftest('pytest_plugins = ["tests.postgres_fixtures"]')
    pytester.makepyfile(test_inner=_INNER_TESTS)

    result = pytester.runpytest("-p", "no:cacheprovider", "-p", "no:asyncio")

    result.assert_outcomes(failed=1, passed=2)
    assert len([line for line in result.outlines if line.startswith("database test_") and " kept: postgresql://" in line]) == 1


def test_a_missing_server_fails_naming_the_make_target(tmp_path: pathlib.Path) -> None:
    empty = tmp_path / "install"
    empty.mkdir()

    with pytest.raises(PostgresInstallMissingError, match=r"no Postgres server under .*/install: `make postgres` exports"):
        PostgresServer(empty, tmp_path / "server")


_ANOTHER_SESSION = """
def test_a_temporary_directory(tmp_path):
    assert tmp_path.is_dir()
"""


def test_the_workers_server_keeps_serving_after_another_pytest_session_makes_its_temporary_directory(
    postgres_database: PostgresDatabase, tmp_path: pathlib.Path
) -> None:
    # A pytest process given no --basetemp sets `pytest-of-<user>` back to 0700 as it makes its temporary directory, as a
    # test that runs pytest in a subprocess does. A checkpoint opens the server's control file by its absolute path, the
    # first thing a server that can no longer reach its data directory fails at. The case this pins is the suite run as
    # root (the CI lane), where the server runs as `nobody`.
    probe = tmp_path / "test_another_session.py"
    probe.write_text(_ANOTHER_SESSION, encoding="utf-8")
    session = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(probe)], cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False
    )

    with psycopg.connect(postgres_database.url, autocommit=True) as connection:
        connection.execute("CHECKPOINT")
        served = connection.execute("SELECT current_database()").fetchone()

    assert (session.returncode, served) == (0, (postgres_database.name,)), session.stdout
