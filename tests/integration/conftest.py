"""Integration fixtures.

The backend namespace is a ``MagicMock(spec=LanceNamespace)`` injected via
dependency override, so these tests exercise *our* layer only — routing,
identifier parsing, request assembly, serialization, and error mapping — never
lance's actual operations.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import DescribeTableResponse, LanceNamespace


@pytest.fixture
def fake_ns() -> MagicMock:
    return MagicMock(spec=LanceNamespace)


@pytest.fixture
def real_ns_client(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient whose namespace is a REAL pylance ``dir`` backend rooted at a tmp dir.

    Needed for the create path: every create now routes through the direct 2.2 + stable-row-ids write
    (``dataplane.create_table`` -> ``declare`` + real ``lance.write_dataset``), so a MagicMock ns can
    no longer stand in — the write actually happens. This also makes the create tests STRONGER: "backend
    create fails" becomes a real create-then-recreate conflict, and "overwrite" a real overwrite, instead of
    a mocked return value. It is exactly the migration the 2.1->2.2 fix required — the mock-only tests could
    never have caught that plain tables were silently landing at format 2.1.
    """
    from lance_namespace import connect

    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", str(tmp_path))
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "test")

    from catalog.core.config import get_settings

    get_settings.cache_clear()
    ns = connect("dir", {"root": str(tmp_path)})

    from catalog.api.dependencies import get_namespace, get_storage_options
    from catalog.main import app

    app.dependency_overrides[get_namespace] = lambda: ns
    app.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def client(fake_ns: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    # A local root keeps the lifespan's build_namespace cheap; requests use the
    # injected fake regardless (get_namespace is overridden). monkeypatch.setenv
    # restores the environment on teardown so tests stay order-independent.
    #
    # `tmp_path`, not a fixed `/tmp/lance-test-root`: the environment was restored on teardown but the
    # DIRECTORY was not, so real catalog state accumulated in one path shared across runs, across
    # concurrent runs, and across users on the same host — which is how a run passes because of what a
    # previous run left behind. Pinned by tests/unit/test_no_fixed_tmp_roots.py.
    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", str(tmp_path))
    # Object-store credentials are required by Settings; the local-dir backend
    # ignores them, but they must be set for Settings() to construct.
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "test")

    from catalog.core.config import get_settings

    get_settings.cache_clear()

    from catalog.api.dependencies import get_namespace, get_storage_options
    from catalog.main import app

    app.dependency_overrides[get_namespace] = lambda: fake_ns
    app.dependency_overrides[get_storage_options] = lambda: {}
    # THE DOUBLE DESCRIBES A REAL LOCATION, as the backend it stands in for does. A data door judges the
    # table's declared bases on the dataset it describes before handing the op to the backend
    # ([[LH-279]]), so a location that opens nothing would answer 404 where the backend is under test.
    # One empty-based table serves every id; a test that describes something else still overrides it.
    described = tmp_path / "described.lance"
    lance.write_dataset(pa.table({"id": pa.array([0], pa.int64())}), str(described), data_storage_version="2.2", enable_stable_row_ids=True)
    fake_ns.describe_table.return_value = DescribeTableResponse(location=str(described), table_uri=str(described))
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()
