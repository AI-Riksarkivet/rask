"""The suite's Postgres fixtures, a pytest plugin the root `conftest.py` loads (`pytest_plugins`).

A plugin rather than root-conftest code, so a test can run a pytest session of its own against these fixtures
(`pytester`) and observe what a failing and a passing test leave behind.
"""

from collections.abc import Generator, Iterator
from typing import Final

import pytest

from tests.postgres_server import POSTGRES_INSTALL, PostgresDatabase, PostgresInstallMissingError, PostgresServer


@pytest.fixture(scope="session")
def postgres(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PostgresServer]:
    """One Postgres per worker, holding the template each test's database is cloned from (`postgres_database`).

    Missing, the fixture fails rather than skips, so a lane without the server reports the gap.
    """
    try:
        server = PostgresServer(POSTGRES_INSTALL, tmp_path_factory.mktemp("postgres"))
    except PostgresInstallMissingError as missing:
        pytest.fail(str(missing))
    try:
        server.build_template()
        yield server
    finally:
        server.stop()


#: The database a test was given, read by the report hook below to name it when the test fails.
_POSTGRES_DATABASE: Final = pytest.StashKey[PostgresDatabase]()
#: Set once any phase of a test that holds a database failed, so its database is kept.
_POSTGRES_KEEP: Final = pytest.StashKey[bool]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Name a failed test's kept database in its report, where the failure is read."""
    report = yield
    database = item.stash.get(_POSTGRES_DATABASE, None)
    if report.failed and database is not None:
        item.stash[_POSTGRES_KEEP] = True
        report.sections.append(("postgres", f"database {database.name} kept: {database.url}"))
    return report


@pytest.fixture
def postgres_database(request: pytest.FixtureRequest, postgres: PostgresServer) -> Iterator[PostgresDatabase]:
    """A database of this test's own, cloned from the worker's template: dropped when the test passes, kept when it fails.

    A kept database outlives the session in the server's data directory under pytest's base temporary directory (pytest
    keeps the last three runs'): `postgres -D <dir>` with the install's `libs` on `LD_LIBRARY_PATH` serves it again.
    """
    database = postgres.create_database()
    request.node.stash[_POSTGRES_DATABASE] = database
    yield database
    if not request.node.stash.get(_POSTGRES_KEEP, False):
        postgres.drop_database(database)
