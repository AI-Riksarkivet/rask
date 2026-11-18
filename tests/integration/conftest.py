"""Integration fixtures.

``client`` injects a ``MagicMock(spec=LanceNamespace)`` via dependency override, so the tests on it
exercise *our* layer only — routing, identifier parsing, request assembly, serialization, and error
mapping — never lance's actual operations. ``real_ns_client`` runs the real ``dir`` namespace on
``tmp_path``, and ``moto_url`` is an S3 endpoint the native Lance and Arrow clients can reach. ``estate`` is the catalog
as deployed over ``moto_url``: every caller signed in through a loopback Dex, authorization on a real OpenFGA store of
the test's own, a declaration mounted (``Estate.declare``) and the converge tick run on the app's own loop
(``Estate.tick``); ``open_estate`` is the same catalog authenticating nobody.
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import boto3
import lance
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import DescribeNamespaceResponse, DescribeTableResponse, LanceNamespace
from moto.server import ThreadedMotoServer
from pydantic import BaseModel

from catalog.api import declaration_converge
from catalog.api.ticks import Tick
from catalog.services import warehouses
from lakehouse.naming import UUID_PROPERTY
from lakehouse.storage_options import StorageOptions
from lakehouse.store_session import IssuedSession


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

#: Modules whose claim is a refusal only a real store's policy makes: collected only where the lane names the store, as
#: ``race_catalog``'s ``s3`` case is, so the offline suite collects neither and skips nothing.
collect_ignore = [] if _RACE_STORE else ["test_the_store_refuses_a_put_over_a_committed_file.py"]


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
    #: What a stock client's own Lance writes the root with beside what the catalog vends it: nothing. On the local root
    #: the catalog vends nothing and none is needed; on the lane's store the client writes with the credential the
    #: catalog vends it (:func:`_vending_session`) and no other key.
    stock_options: dict[str, str]


def _catalog_policy() -> dict[str, object]:
    """The canned policy the chart grants the catalog's own identity on the store (`lance.objectStorePolicy.catalog`), as
    `helm template` renders it into the identities ConfigMap the provisioning hook reads: every vend is a session of it,
    narrowed by the session policy the catalog writes, so the seal's Deny there is the one the store applies to every
    vend (ADR 0242). Rendered, never copied: a lane that held its own copy would pass with the chart's Deny removed."""
    from tests.unit import chart_render

    rendered = chart_render.render(*chart_render.DEFAULT_ARGS)
    identities = next(doc for doc in rendered if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"].endswith("-object-store-identities"))
    return json.loads(identities["data"]["rask-sa-catalog.json"])


def _vending_session() -> Callable[..., IssuedSession]:
    """The store's STS exchange as the catalog's vend makes it, for a lane with no token issuer: an identity that holds the
    catalog's canned policy (a user of the lane's own, made now) assumes a session narrowed by the vend's session policy,
    for the length the vend asks. The store, not the catalog, then decides what each request on the vended session may
    do (`.claude/skills/rask-rustfs`: AssumeRole by a non-root user, the session policy intersected with the parent's)."""
    import boto3
    from botocore.config import Config
    from pydantic import SecretStr

    from lakehouse.governed.store_credential import S3KeyPair

    spec = importlib.util.spec_from_file_location("rustfs_admin", Path(__file__).resolve().parents[2] / "scripts" / "rustfs_admin.py")
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    admin = script.AdminClient(_RACE_STORE, os.environ["RASK_LIFECYCLE_STORE_ACCESS_KEY"], os.environ["RASK_LIFECYCLE_STORE_SECRET_KEY"])
    user, secret = f"rask-vend-{uuid4().hex[:8]}", secrets.token_urlsafe(24)
    admin.put("add-canned-policy", {"name": "rask-lifecycle-catalog"}, json.dumps(_catalog_policy()).encode())
    admin.put("add-user", {"accessKey": user}, json.dumps({"secretKey": secret, "status": "enabled"}).encode())
    admin.put("set-user-or-group-policy", {"policyName": "rask-lifecycle-catalog", "userOrGroup": user, "isGroup": "false"}, b"")
    sts = boto3.client(
        "sts",
        endpoint_url=_RACE_STORE,
        region_name="us-east-1",
        aws_access_key_id=user,
        aws_secret_access_key=secret,
        config=Config(retries={"max_attempts": 1}),
    )

    def exchange(_identity: object, *, session_name: str, duration_seconds: int, policy: str | None = None) -> IssuedSession:
        asked: dict[str, object] = {"RoleArn": "arn:aws:iam::000000000000:role/vend", "RoleSessionName": session_name, "DurationSeconds": duration_seconds}
        if policy is not None:
            asked["Policy"] = policy
        issued = sts.assume_role(**asked)["Credentials"]
        key_pair = S3KeyPair(
            access_key_id=issued["AccessKeyId"], secret_access_key=SecretStr(issued["SecretAccessKey"]), session_token=SecretStr(issued["SessionToken"])
        )
        return IssuedSession(key_pair=key_pair, expiration=issued["Expiration"])

    return exchange


@pytest.fixture(params=["dir", "s3"] if _RACE_STORE else ["dir"])
def race_catalog(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RaceCatalog]:
    """The catalog app under a live uvicorn (`lineage_app.live_catalog`), for a race a stock Lance client takes part in, on
    the local root and, where a lane names an S3 store, in a bucket of the test's own there, as ``race_ns_client``, the
    catalog vending each stock client a session the store's STS issues (:func:`_vending_session`)."""
    from tests.integration.lineage_app import live_catalog

    env = _race_bucket() if request.param == "s3" else {}
    with live_catalog(tmp_path / "catalog", monkeypatch, **env) as url:
        from catalog.api.dependencies import get_vendor
        from catalog.core.vending import WebIdentityVendor
        from catalog.main import app
        from lakehouse.store_session import WebIdentity

        if env:
            # The stock client signs with what the catalog vends it and nothing ambient.
            for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
                monkeypatch.delenv(key, raising=False)
            identity = WebIdentity(token_file=tmp_path / "no-token", endpoint=_RACE_STORE, region="us-east-1", role_arn="arn:aws:iam::000000000000:role/vend")
            vendor = WebIdentityVendor(identity=identity, store_endpoint=_RACE_STORE, exchange=_vending_session())
            app.dependency_overrides[get_vendor] = lambda: vendor
        try:
            yield RaceCatalog(url=url, stock_options={})
        finally:
            app.dependency_overrides.pop(get_vendor, None)


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


#: The estate's own bucket, the people's Dex connector and the idp it maps to, and one of the release's own accounts
#: (``LANCE_OWN_ACCOUNTS``), as :func:`estate` deploys the catalog.
ESTATE_BUCKET = "estate"
CONNECTOR, IDP = "local", "dex"
MAINTENANCE = "kubernetes~system:serviceaccount:default:rask-sa-maintenance"


class Estate:
    """The catalog as deployed with a declaration mounted, and what a test reads it back through."""

    def __init__(self, client: TestClient, store: Any, dex: Any, declaration: Path) -> None:  # noqa: ANN401 — root conftest's OpenFgaStore and DexIssuer
        self.client = client
        self.store = store
        self._dex = dex
        self._declaration = declaration

    def declare(self, **declared: Any) -> None:
        """Replace the declaration, as a reconcile of the chart's ConfigMap does."""
        self._declaration.write_text(json.dumps(declared))

    def tick(self) -> declaration_converge.ConvergeTick:
        """One converge tick on the catalog app's own event loop, as its timer runs one; what the tick did."""
        app = cast(FastAPI, self.client.app)
        assert self.client.portal is not None, "the converge ticks inside the app's lifespan"
        return self.client.portal.call(declaration_converge.tick, Tick(app, app.state.settings))

    def bearer(self, user_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._dex.mint(user_id, connector=CONNECTOR, audience='lance-catalog')}"}

    def record(self, warehouse_id: str) -> dict[str, str] | None:
        settings = self.client.app.state.settings
        return warehouses.get_warehouse(settings.registry_root, settings.storage_options(), warehouse_id)


@contextmanager
def _catalog(moto_url: str, declaration: Path, monkeypatch: pytest.MonkeyPatch, governed: dict[str, str]) -> Generator[TestClient]:
    """The catalog over an emptied S3 store, ``declaration`` mounted (empty), under ``governed``'s auth settings."""
    s3 = boto3.resource("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    for bucket in s3.buckets.all():
        bucket.objects.all().delete()
        bucket.delete()
    s3.create_bucket(Bucket=ESTATE_BUCKET)
    declaration.write_text("{}")
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{ESTATE_BUCKET}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_DECLARATION_FILE": str(declaration),
        **governed,
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


@pytest.fixture
def estate(moto_url: str, openfga_store: Any, dex_issuer: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Estate]:  # noqa: ANN401 — root conftest's fixtures
    """The catalog as deployed: every caller authenticated, authorization on a real OpenFGA store of the test's own."""
    governed = {
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": dex_issuer.issuer,
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_OIDC_ALLOW_INSECURE": "true",
        "RASK_OIDC_IDP_ID": "dex",
        "RASK_OIDC_CONNECTORS": json.dumps({CONNECTOR: IDP}),
        "RASK_FGA_ENABLED": "true",
        "RASK_FGA_API_URL": openfga_store.url,
        "RASK_FGA_STORE_ID": openfga_store.store_id,
        "RASK_FGA_MODEL_ID": openfga_store.model_id,
        "LANCE_OWN_ACCOUNTS": json.dumps([MAINTENANCE]),
    }
    declaration = tmp_path / "declaration.json"
    with _catalog(moto_url, declaration, monkeypatch, governed) as client:
        yield Estate(client, openfga_store, dex_issuer, declaration)


@pytest.fixture
def open_estate(moto_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Estate]:
    """The catalog on a stack that authenticates nobody: no caller, no authorization store."""
    declaration = tmp_path / "declaration.json"
    with _catalog(moto_url, declaration, monkeypatch, {"RASK_OIDC_ENABLED": "false", "RASK_FGA_ENABLED": "false"}) as client:
        yield Estate(client, None, None, declaration)
