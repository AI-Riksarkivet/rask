"""Integration fixtures.

``client`` injects a ``MagicMock(spec=LanceNamespace)`` via dependency override, so the tests on it
exercise *our* layer only — routing, identifier parsing, request assembly, serialization, and error
mapping — never lance's actual operations. ``real_ns_client`` runs the real ``dir`` namespace on
``tmp_path``, and ``moto_url`` is an S3 endpoint the native Lance and Arrow clients can reach.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import DescribeNamespaceResponse, DescribeTableResponse, LanceNamespace
from moto.server import ThreadedMotoServer
from pydantic import BaseModel

from lakehouse.naming import UUID_PROPERTY
from lakehouse.storage_options import StorageOptions


@pytest.fixture(scope="module")
def moto_url() -> Iterator[str]:
    """A moto S3 server for one test module: ``mock_aws()`` patches botocore, which pylance and pyarrow never go through."""
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


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
    app.dependency_overrides[get_storage_options] = lambda: StorageOptions()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


#: The S3 store the race tests also run on, named by the lane that stands it up (`scripts/lifecycle_on_rustfs_check.sh`):
#: the offline suite names none, and collects only the local root.
_RACE_STORE = os.environ.get("RASK_LIFECYCLE_STORE_ENDPOINT", "")


def _race_bucket() -> dict[str, str]:
    """A bucket of the test's own on the lane's S3 store, made now; the catalog's settings that root it there."""
    import boto3

    bucket = f"race-{uuid4().hex[:12]}"
    access, secret = os.environ["RASK_LIFECYCLE_STORE_ACCESS_KEY"], os.environ["RASK_LIFECYCLE_STORE_SECRET_KEY"]
    store = boto3.client("s3", endpoint_url=_RACE_STORE, region_name="us-east-1", aws_access_key_id=access, aws_secret_access_key=secret)
    store.create_bucket(Bucket=bucket)
    return {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": _RACE_STORE,
        "LANCE_S3_ACCESS_KEY_ID": access,
        "LANCE_S3_SECRET_ACCESS_KEY": secret,
        "LANCE_S3_REGION": "us-east-1",
    }


class RaceCatalog(BaseModel):
    """The live catalog a race between a stock client and the catalog's doors runs against."""

    url: str
    #: What a stock client's own Lance writes the root with: nothing on the local root; on the lane's store its key pair,
    #: since the catalog vends no credential where it runs without a web identity (``LANCE_VENDING_MODE=mode_b``).
    stock_options: dict[str, str]


@pytest.fixture(params=["dir", "s3"] if _RACE_STORE else ["dir"])
def race_catalog(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RaceCatalog]:
    """The catalog app under a live uvicorn (`lineage_app.live_catalog`), for a race a stock Lance client takes part in, on
    the local root and, where a lane names an S3 store, in a bucket of the test's own there, as ``race_ns_client``."""
    from tests.integration.lineage_app import live_catalog

    env = _race_bucket() if request.param == "s3" else {}
    with live_catalog(tmp_path / "catalog", monkeypatch, **env) as url:
        from catalog.core.config import get_settings

        options = get_settings().storage_options().in_clear() if env else {}
        yield RaceCatalog(url=url, stock_options=options)


@pytest.fixture(params=["dir", "s3"] if _RACE_STORE else ["dir"])
def race_ns_client(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """``real_ns_client`` for a test of a race between doors, whose every exclusion is a conditional write on the store:
    on the local root, and, where a lane names an S3 store, with the catalog's root and control root in a bucket of the
    test's own there, so the store's put-if-not-exists and If-Match arbitrate each fence, claim and record."""
    if request.param == "dir":
        yield request.getfixturevalue("real_ns_client")
        return
    for key, value in _race_bucket().items():
        monkeypatch.setenv(key, value)
    from catalog.api.dependencies import get_namespace
    from catalog.core.config import get_settings
    from catalog.core.namespace import build_namespace
    from catalog.main import app

    get_settings.cache_clear()
    ns = build_namespace(get_settings())
    app.dependency_overrides[get_namespace] = lambda: ns
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
    app.dependency_overrides[get_storage_options] = lambda: StorageOptions()
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
    from lakehouse import location_claims

    def _claim(table: str) -> str:
        minted = str(uuid4())
        location = fake_ns.describe_table.return_value.location
        location_claims.take(table_claims.claim_store(get_settings()), location, table, table.split("$"), is_gone=lambda _claim: False, uuid=minted)
        return minted

    return _claim
