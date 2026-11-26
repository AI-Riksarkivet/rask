"""Integration fixtures.

``client`` injects a ``MagicMock(spec=LanceNamespace)`` via dependency override, so the tests on it
exercise *our* layer only — routing, identifier parsing, request assembly, serialization, and error
mapping — never lance's actual operations. ``real_ns_client`` runs the real ``dir`` namespace on
``tmp_path``. The S3 endpoint the native Lance and Arrow clients can reach, ``moto_url``, is the root conftest's.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import DescribeNamespaceResponse, DescribeTableResponse, LanceNamespace
from pydantic import SecretStr

from lakehouse.naming import UUID_PROPERTY
from lakehouse.storage.credential import S3KeyPair
from lakehouse.storage.options import StorageOptions
from tests.governed_catalog import GovernedCatalog, governed_catalog_app


@pytest.fixture
def fake_ns() -> MagicMock:
    return MagicMock(spec=LanceNamespace)


@pytest.fixture
def real_ns_client(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient whose namespace is a REAL pylance ``dir`` backend rooted at a tmp dir.

    Needed for the create path: every create now routes through the direct 2.2 + stable-row-ids write
    (``tables.creation.create_table`` -> ``declare`` + real ``lance.write_dataset``), so a MagicMock ns can
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
    app.dependency_overrides[get_storage_options] = lambda: StorageOptions()
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
    # previous run left behind.
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
    # A whole pair, as the catalog's own options always carry: a location the double describes on S3 builds no client
    # from options without one.
    app.dependency_overrides[get_storage_options] = lambda: StorageOptions(key_pair=S3KeyPair(access_key_id="test", secret_access_key=SecretStr("test")))
    # THE DOUBLE DESCRIBES A REAL LOCATION, as the backend it stands in for does. A data door judges the
    # table's declared bases on the dataset it describes before handing the op to the backend
    # ([[LH-279]]), so a location that opens nothing would answer 404 where the backend is under test.
    # One empty-based table serves every id; a test that describes something else still overrides it.
    described = tmp_path / "described.lance"
    lance.write_dataset(pa.table({"id": pa.array([0], pa.int64())}), str(described), data_storage_version="2.2", enable_stable_row_ids=True)
    fake_ns.describe_table.return_value = DescribeTableResponse(location=str(described), table_uri=str(described))
    # A namespace declaring no tier: every write, a write-tier vend among them, reads its tier before it is judged. It
    # carries the uuid its create minted, as every namespace a door made does (ADR 0173 §2).
    fake_ns.describe_namespace.return_value = DescribeNamespaceResponse(properties={UUID_PROPERTY: str(uuid4())})
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def claim_described(client: TestClient, fake_ns: MagicMock) -> Callable[[str], str]:
    """Take, for a table, the claim the door that made it took on the location ``client``'s double describes, and
    return the uuid it records (ADR 0173 §2): a door keeping a record for a table, or acting on it, reads it there."""
    from catalog.core.config import get_settings
    from catalog.services import table_claims
    from lakehouse.records import location_claims

    def _claim(table: str) -> str:
        minted = str(uuid4())
        location = fake_ns.describe_table.return_value.location
        location_claims.take(table_claims.claim_store(get_settings()), location, table, segments=table.split("$"), is_gone=lambda _claim: False, uuid=minted)
        return minted

    return _claim


@pytest.fixture
def governed_catalog(openfga_store: Any, dex_issuer: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[GovernedCatalog]:  # noqa: ANN401 — the root conftest's fixtures
    """The catalog on a real ``dir`` namespace, signed-in callers only, each decision OpenFGA's on a store of this test's own."""
    with governed_catalog_app(openfga_store, dex_issuer, tmp_path, monkeypatch, held_namespace=True) as catalog:
        yield catalog
