"""A catalog refuses to start against a database schema behind its own revision or at one it does not know, and says
why in one line (ADR 0230 §11): no code of this image is right for either, and retrying never makes a newer schema older.

The database is the test's own, migrated as the release migrates it; each case stages the other side through the
server's superuser, as a release that never ran its migration, or a newer one that did, leaves it.
"""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import LiteralString

import psycopg
import pytest
from fastapi.testclient import TestClient

from catalog.core.schema import SchemaMismatchError
from tests.postgres_server import PostgresDatabase


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """What a catalog app needs beside its database, on a local root."""
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("staging", "refusal"),
    [
        pytest.param(
            "DROP SCHEMA rask CASCADE",
            "the catalog's database schema is at revision none and this image needs 0001: the migration Job has not applied it",
            id="behind",
        ),
        pytest.param(
            "UPDATE rask.alembic_version SET version_num = '9999'",
            "the catalog's database schema is at revision 9999, which this image does not know (its newest is 0001): a newer "
            "release migrated this database; run a release at least as new",
            id="ahead",
        ),
    ],
)
def test_a_catalog_refuses_to_start_against_a_schema_behind_or_ahead_of_its_own(
    catalog_database: PostgresDatabase, catalog_env: None, staging: LiteralString, refusal: str
) -> None:
    with psycopg.connect(catalog_database.url, autocommit=True) as superuser:
        superuser.execute(staging)
    from catalog.main import app

    with pytest.raises(SchemaMismatchError, match=f"^{re.escape(refusal)}$"), TestClient(app):
        pass
