"""A table's maintenance pass deletes nothing a door answered 200 for (ADR 0243, ``specs/maintenance/TablePass.tla``).

Each test drives one finding of the pass's model through the real maintenance pass (``table_pass.run_table_pass``) and
the real catalog app on one S3 store: the pass is held at the step the model's trace names (its trash recheck, or its
cleanup's first delete, held on the wire by a proxy in front of the store, since Lance's cleanup has no Python hook),
the other door runs whole, the pass is let go, and the claim is judged by what a reader then opens. A cleanup whose
credential expires part-way is the store answering its manifest deletes 403 ``ExpiredToken``.

Each pins the guard ADR 0243 names for its finding, and where two guards sit at one seam the test is parametrized by
where it holds the pass. Each runs on moto, and on the pinned RustFS where ``make lifecycle-rustfs-check`` names it; a
local root has no wire to hold a cleanup on.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import httpx
import lance
import pyarrow as pa
import pytest
import respx
from fastapi.testclient import TestClient
from moto.server import ThreadedMotoServer
from pydantic import BaseModel, SecretStr

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.objectfs import lance_storage_options
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.services import expiry
from maintenance.services.table_pass import PassOutcome, TableTask, run_table_pass
from tests.integration.maintenance_vend_doors import CATALOG_URL, MAINTAIN_TIER_DOOR
from tests.integration.store_requests import CountedStore, StoreRequest, counted


NS = "lance_catalog$db"
T = f"{NS}$t"
ARROW = {"Content-Type": ARROW_STREAM_MEDIA_TYPE}
#: How long a step waits for the one it is interleaved with: far above what either takes, so it only bounds a hang.
WAIT = 60
#: The lane's RustFS, where `scripts/lifecycle_on_rustfs_check.sh` stands one up.
_RUSTFS = os.environ.get("RASK_LIFECYCLE_STORE_ENDPOINT", "")


class _Store(BaseModel):
    """An S3 store, and the key pair every party here signs with: the catalog's, the pass's vend's and the reader's."""

    endpoint: str
    access_key: str
    secret_key: str

    def options(self, endpoint: str | None = None) -> StorageOptions:
        return lance_storage_options(
            endpoint or self.endpoint, S3KeyPair(access_key_id=self.access_key, secret_access_key=SecretStr(self.secret_key)), "us-east-1"
        )


@pytest.fixture(scope="module")
def moto_url() -> Iterator[str]:
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture(params=["moto", "rustfs"] if _RUSTFS else ["moto"])
def store(request: pytest.FixtureRequest) -> _Store:
    if request.param == "rustfs":
        return _Store(endpoint=_RUSTFS, access_key=os.environ["RASK_LIFECYCLE_STORE_ACCESS_KEY"], secret_key=os.environ["RASK_LIFECYCLE_STORE_SECRET_KEY"])
    return _Store(endpoint=request.getfixturevalue("moto_url"), access_key="test", secret_key="test")


class _Estate(BaseModel):
    """The catalog over a bucket of the test's own, the proxy the pass reaches the store through, and the table."""

    model_config = {"arbitrary_types_allowed": True}

    catalog: TestClient
    proxy: CountedStore
    store: _Store
    bucket: str
    task: TableTask

    def settings(self) -> MaintenanceSettings:
        from maintenance.core.config import get_settings

        get_settings.cache_clear()
        return get_settings()

    def opened(self, version: int | str | None = None) -> lance.LanceDataset:
        """The table as a reader opens it, at ``version`` (a number or a tag), on the store directly."""
        ds = lance.dataset(self.task.location, storage_options=self.store.options().in_clear())
        return ds if version is None else ds.checkout_version(version)

    def reads(self, version: int | str | None = None) -> list[int] | str:
        """The ids the table holds at ``version``, or why a reader could not read them."""
        try:
            return sorted(self.opened(version).to_table().column("id").to_pylist())
        except Exception as exc:  # noqa: BLE001 — what a reader meets is the claim judged
            return f"{type(exc).__name__}: {exc}"[:200]

    def passed(self) -> PassOutcome:
        """The table's pass, run whole, with its maintain vend answered as the catalog answers it: the store's pair, over
        the table's location, at the address off the cluster that maintenance replaces with its own (the proxy)."""

        def _vend(_request: httpx.Request, table: str) -> httpx.Response:
            vended = {"aws_access_key_id": self.store.access_key, "aws_secret_access_key": self.store.secret_key, "aws_endpoint": "http://store.vended.example"}
            return httpx.Response(200, json={"mode": "direct", "credentials": {"storage_options": vended}, "location": self.task.location})

        with respx.mock(assert_all_called=False) as router:
            router.post(url__regex=MAINTAIN_TIER_DOOR, params={"tier": "maintain"}).mock(side_effect=_vend)
            router.route(host="testserver").pass_through()
            return run_table_pass(self.task, self.settings())


def _rows(ids: list[int]) -> bytes:
    return encode_arrow_stream(pa.table({"id": pa.array(ids, pa.int64())}))


def _ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def estate(store: _Store, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Estate]:
    """Table ``T`` at three versions, each an overwrite (ids [1], [2], [3]), under its own policy keeping one version: a
    pass reclaims versions 1 and 2."""
    bucket = f"pass-{uuid.uuid4().hex[:10]}"
    boto3.client(
        "s3", endpoint_url=store.endpoint, aws_access_key_id=store.access_key, aws_secret_access_key=store.secret_key, region_name="us-east-1"
    ).create_bucket(Bucket=bucket)
    with counted(store.endpoint) as proxy:
        for key, value in {
            "LANCE_REST_IMPL": "dir",
            "LANCE_REST_ROOT": f"s3://{bucket}",
            "LANCE_CONTROL_ROOT": f"s3://{bucket}",
            "LANCE_S3_ENDPOINT": store.endpoint,
            "LANCE_S3_ACCESS_KEY_ID": store.access_key,
            "LANCE_S3_SECRET_ACCESS_KEY": store.secret_key,
            "LANCE_S3_REGION": "us-east-1",
            "LANCE_CONTROL_EMIT_ENABLED": "false",
            "MAINTENANCE_S3_ENDPOINT": proxy.url,
            "MAINTENANCE_S3_ACCESS_KEY_ID": store.access_key,
            "MAINTENANCE_S3_SECRET_ACCESS_KEY": store.secret_key,
            "MAINTENANCE_S3_BUCKET": bucket,
            "MAINTENANCE_MIN_VERSIONS_TO_EXPIRE": "1",
            "MAINTENANCE_CATALOG_URL": CATALOG_URL,
        }.items():
            monkeypatch.setenv(key, value)
        from catalog.api.dependencies import get_namespace
        from catalog.core.config import get_settings
        from catalog.core.namespace import build_namespace
        from catalog.main import app

        get_settings.cache_clear()
        ns = build_namespace(get_settings())
        app.dependency_overrides[get_namespace] = lambda: ns
        try:
            with TestClient(app) as catalog:
                _ok(catalog.post(f"/v1/namespace/{NS}/create", json={}))
                _ok(catalog.post(f"/v1/table/{T}/create?mode=create", content=_rows([1]), headers=ARROW))
                for ids in ([2], [3]):
                    _ok(catalog.post(f"/v1/table/{T}/insert?mode=overwrite", content=_rows(ids), headers=ARROW))
                _ok(catalog.post(f"/management/v1/table/{T}/policy/set", json={"retain_versions": 1}))
                described = _ok(catalog.post(f"/v1/table/{T}/describe", json={}))
                task = TableTask(uuid=described["properties"]["rask.uuid"], location=described["location"])
                yield _Estate(catalog=catalog, proxy=proxy, store=store, bucket=bucket, task=task)
        finally:
            app.dependency_overrides.clear()
            get_settings.cache_clear()
            from maintenance.core.config import get_settings as maintenance_settings

            maintenance_settings.cache_clear()


class _Running:
    """The table's pass run in a thread of its own, to be held at a step and joined once let go."""

    def __init__(self, estate: _Estate) -> None:
        self.outcome: list[PassOutcome | BaseException] = []
        self._thread = threading.Thread(target=self._run, args=(estate,), daemon=True)
        self._thread.start()

    def _run(self, estate: _Estate) -> None:
        try:
            self.outcome.append(estate.passed())
        except BaseException as exc:  # noqa: BLE001 — joined and raised below
            self.outcome.append(exc)

    def joined(self) -> PassOutcome:
        self._thread.join(timeout=WAIT)
        assert not self._thread.is_alive(), "the pass did not end"
        (outcome,) = self.outcome
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _a_cleanup_delete(request: StoreRequest) -> bool:
    """A delete Lance's cleanup sends: of a transaction file (its first, measured), a data file or a manifest."""
    return any(request.deletes(directory) for directory in ("/_transactions/", "/data/", "/_versions/"))


def _held_before(monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[threading.Event, threading.Event]:
    """Hold the pass as it calls ``expiry.<name>``: the event set once it reached it, and the one that lets it on."""
    reached, let_go = threading.Event(), threading.Event()
    original = getattr(expiry, name)

    def _held(*args: object, **kwargs: object) -> object:
        reached.set()
        assert let_go.wait(WAIT), "the test never let the pass on"
        return original(*args, **kwargs)

    monkeypatch.setattr(expiry, name, _held)
    return reached, let_go


def test_a_restore_answered_200_names_no_file_a_reclaim_beside_it_deletes(estate: _Estate) -> None:
    """RestoreBesideReclaim: the pass's cleanup has listed the table's versions and refs and is held at its first delete;
    a restore of version 1, which the cleanup is removing, lands meanwhile. Let go, the cleanup deletes version 1's data
    files, which the restored head now names. The guard (RestoreFence): the restore holds the table's history as a ref
    write does, which a reclaim excludes, so it answers 409 while the pass deletes."""
    with estate.proxy.holding(_a_cleanup_delete) as held:
        running = _Running(estate)
        assert held.reached(WAIT), "the pass's cleanup deleted nothing"
        restored = estate.catalog.post(f"/v1/table/{T}/restore", json={"version": 1})
        held.let_go()
        running.joined()

    assert restored.status_code in (200, 409), restored.text
    assert restored.status_code != 200 or estate.reads() == [1], f"the restore answered 200 and the head reads {estate.reads()}"


@pytest.mark.parametrize("held_at", ["before-its-hold", "at-its-cleanups-first-delete"])
def test_a_policy_keeping_the_whole_history_answered_200_keeps_every_version(estate: _Estate, monkeypatch: pytest.MonkeyPatch, held_at: str) -> None:
    """PolicyReadBeforeHold: the pass read the table's policy (keep one version) as it started; the owner then sets a
    policy keeping the whole history, which answers 200, and the pass, let on, reclaims versions 1 and 2 under the policy
    it read. Two guards, one a case: held before its hold, the pass reads the policy again once it holds the table's
    history and deletes nothing when it changed (PolicyReread); held inside its cleanup, the policy door holds the
    table's history as a ref write does while it writes, so it answers 409 beside a reclaim (PolicyDoorFence)."""
    standing = [entry["version"] for entry in estate.opened().versions()]

    def _door() -> httpx.Response:
        return estate.catalog.post(f"/management/v1/table/{T}/policy/set", json={"cleanup_enabled": False})

    if held_at == "before-its-hold":
        reached, let_go = _held_before(monkeypatch, "_history_held")
        running = _Running(estate)
        assert reached.wait(WAIT), "the pass never came to its hold"
        answered = _door()
        let_go.set()
        running.joined()
    else:
        with estate.proxy.holding(_a_cleanup_delete) as held:
            running = _Running(estate)
            assert held.reached(WAIT), "the pass's cleanup deleted nothing"
            answered = _door()
            held.let_go()
            running.joined()

    kept = [entry["version"] for entry in estate.opened().versions()]
    assert answered.status_code in (200, 409), answered.text
    assert answered.status_code != 200 or kept == standing, f"the policy answered 200 over versions {standing}, and the pass left {kept}"


@pytest.mark.parametrize("door", ["tag", "restore"])
def test_a_ref_answered_200_names_a_version_a_reader_can_read_whole(estate: _Estate, door: str) -> None:
    """HalfRemoved: Lance's cleanup deletes the data files of the versions it removes before their manifests (measured on
    pylance 13.0.0), and the pass's credential expires between the two, so the store refuses the manifest deletes:
    versions 1 and 2 still stand, over deleted data. A tag on version 1, or a restore of it, then answers 200 for a
    version no reader can read. The guard (WholeRef): a ref door, holding the table's history, refuses a version whose
    files are not all stored."""
    with estate.proxy.answering_expired(lambda request: request.deletes("/_versions/")):
        estate.passed()
    assert 1 in [entry["version"] for entry in estate.opened().versions()], "the pass removed version 1 whole, so nothing stands half-removed"

    if door == "tag":
        answered = estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "kept", "version": 1})
        read = estate.reads("kept")
    else:
        answered = estate.catalog.post(f"/v1/table/{T}/restore", json={"version": 1})
        read = estate.reads()

    assert answered.status_code in (200, 409), answered.text
    assert answered.status_code != 200 or read == [1], f"the {door} answered 200 for version 1, which reads {read}"
