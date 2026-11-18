"""A table's maintenance pass deletes nothing a door answered 200 for (ADR 0243, ``specs/maintenance/TablePass.tla``).

Each test drives one finding of the pass's model through the real maintenance pass (``table_pass.run_table_pass``) and
the real catalog app on one S3 store: the pass is held at the step the model's trace names (its trash recheck, or its
cleanup's first delete, held on the wire by a proxy in front of the store, since Lance's cleanup has no Python hook),
the other door runs whole, the pass is let go, and the claim is judged by what a reader then opens. A cleanup whose
credential expires part-way is the store answering its manifest deletes 403 ``ExpiredToken``.

Each pins the guard ADR 0243 names for its finding, and where two guards sit at one seam the test is parametrized by
where it holds the pass and which door acts. Each runs on moto, and on the pinned RustFS where ``make
lifecycle-rustfs-check`` names it; a local root has no wire to hold a cleanup on. The catalog reaches the store through
the same proxy as the pass, so a test can also answer the catalog's own reads as a store that cannot serve them.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import httpx
import lance
import pyarrow as pa
import pytest
import respx
from fastapi.testclient import TestClient
from lance import blob_array
from moto.server import ThreadedMotoServer
from pydantic import BaseModel, SecretStr

from lakehouse import fences, history_fences, policy_fences
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

    def versions(self) -> list[int]:
        """The versions main holds now."""
        return [int(entry["version"]) for entry in self.opened().versions()]

    def written(self, count: int) -> None:
        """``count`` more versions on main, each an overwrite."""
        for n in range(count):
            _ok(self.catalog.post(f"/v1/table/{T}/insert?mode=overwrite", content=_rows([100 + n]), headers=ARROW))

    def marks(self) -> list[str]:
        """The keys of the cleanup marks standing in the table's history fence directory."""
        directory = history_fences.fence_directory(self.task.uuid)
        listed = self.s3().list_objects_v2(Bucket=self.bucket, Prefix=f"{directory}/").get("Contents", [])
        return [entry["Key"] for entry in listed if f"/{history_fences.VICTIMS_MARK}." in entry["Key"]]

    def s3(self) -> Any:  # noqa: ANN401 — a boto3 S3 client
        return boto3.client(
            "s3",
            endpoint_url=self.store.endpoint,
            aws_access_key_id=self.store.access_key,
            aws_secret_access_key=self.store.secret_key,
            region_name="us-east-1",
        )

    def reads(self, version: int | str | None = None, *, location: str | None = None) -> list[int] | str:
        """The ids the table (or the dataset at ``location``) holds at ``version``, each row's payload read whole where it
        has one, or why a reader could not read them."""
        try:
            opened = self.opened(version) if location is None else lance.dataset(location, storage_options=self.store.options().in_clear())
            ids = sorted(opened.to_table(columns=["id"]).column("id").to_pylist())
            if "payload" in opened.schema.names:
                for blob in opened.take_blobs("payload", indices=list(range(opened.count_rows()))):
                    assert blob is not None and len(blob.read()) > 0, "a payload read empty"
            return ids
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


def _rows(ids: list[int], payload_bytes: int = 0) -> bytes:
    """The rows ``ids``, each with a payload of ``payload_bytes`` when it is not 0: past Lance's dedicated threshold (4 MiB),
    a blob sidecar of its own beside the data file (``lance_docs/lance/format/table/blob.md``)."""
    columns: dict[str, pa.Array] = {"id": pa.array(ids, pa.int64())}
    if payload_bytes:
        columns["payload"] = blob_array([os.urandom(payload_bytes) for _ in ids])
    return encode_arrow_stream(pa.table(columns))


@pytest.fixture
def payload_bytes() -> int:
    """How large each row's payload is; 0 for a table of ids alone. A test parametrizes it to write blob sidecars."""
    return 0


def _ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def estate(store: _Store, monkeypatch: pytest.MonkeyPatch, payload_bytes: int) -> Iterator[_Estate]:
    """Table ``T`` at three versions, each an overwrite (ids [1], [2], [3], each with a payload of ``payload_bytes``),
    under its own policy keeping one version: a pass reclaims versions 1 and 2."""
    bucket = f"pass-{uuid.uuid4().hex[:10]}"
    boto3.client(
        "s3", endpoint_url=store.endpoint, aws_access_key_id=store.access_key, aws_secret_access_key=store.secret_key, region_name="us-east-1"
    ).create_bucket(Bucket=bucket)
    with counted(store.endpoint) as proxy:
        for key, value in {
            "LANCE_REST_IMPL": "dir",
            "LANCE_REST_ROOT": f"s3://{bucket}",
            "LANCE_CONTROL_ROOT": f"s3://{bucket}",
            "LANCE_S3_ENDPOINT": proxy.url,
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
                _ok(catalog.post(f"/v1/table/{T}/create?mode=create", content=_rows([1], payload_bytes), headers=ARROW))
                for ids in ([2], [3]):
                    _ok(catalog.post(f"/v1/table/{T}/insert?mode=overwrite", content=_rows(ids, payload_bytes), headers=ARROW))
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


#: The namespace a rename moves the table into, whose hold then binds it.
HELD = "lance_catalog$held"
#: The project a case gives the bucket to, so its policy may govern the table.
PROJECT = "acme"

#: Each policy door a case drives, and the request it makes: the table's hold, the table's record deleted (which hands the
#: table to its namespace's, keeping every version), the namespace's hold, the project's hold, and a rename into a
#: namespace that holds.
_POLICY_DOORS = {
    "table-hold": (f"/management/v1/table/{T}/policy/set", {"cleanup_enabled": False}),
    "table-delete": (f"/management/v1/table/{T}/policy/delete", {}),
    "namespace-hold": (f"/management/v1/namespace/{NS}/policy/set", {"cleanup_enabled": False}),
    "project-hold": (f"/management/v1/project/{PROJECT}/policy/set", {"cleanup_enabled": False}),
    "rename-into-a-held-namespace": (f"/v1/table/{T}/rename", {"new_namespace_id": HELD.split("$"), "new_table_name": "t"}),
}


def _governed_by_a_project(estate: _Estate) -> None:
    """The bucket made the one warehouse of project ``PROJECT``, as the catalog records a warehouse's bucket, so the
    project's policy can govern the table."""
    from catalog.core.config import get_settings
    from catalog.services import projects, warehouses
    from catalog.services.control_records import PROJECT_UUID, UUID

    settings = get_settings()
    so = settings.storage_options()
    made = "a" * 32
    projects.put_project(settings.registry_root, so, {"id": PROJECT, "created_at": "t", "created_by": "seed", UUID: made})
    warehouses.put_warehouse(
        settings.registry_root,
        so,
        {"id": "lance_catalog", "bucket": estate.bucket, "project": PROJECT, "status": "active", UUID: "b" * 32, PROJECT_UUID: made},
    )
    claim = {"bucket": estate.bucket, "project": PROJECT, PROJECT_UUID: made, "warehouse_id": "lance_catalog"}
    estate.s3().put_object(Bucket=estate.bucket, Key=f"_warehouses/bucket-claims/{estate.bucket}.json", Body=json.dumps(claim).encode())


@pytest.mark.parametrize(
    ("door", "held_at"),
    [
        ("table-hold", "before-its-hold"),
        ("table-hold", "at-its-cleanups-first-delete"),
        ("table-delete", "at-its-cleanups-first-delete"),
        ("namespace-hold", "before-the-pass"),
        ("namespace-hold", "at-its-cleanups-first-delete"),
        ("namespace-hold", "its-door-stalled-past-its-hold"),
        ("project-hold", "at-its-cleanups-first-delete"),
        ("rename-into-a-held-namespace", "before-its-hold"),
        ("rename-into-a-held-namespace", "at-its-cleanups-first-delete"),
    ],
)
def test_a_policy_keeping_the_whole_history_answered_200_keeps_every_version(estate: _Estate, monkeypatch: pytest.MonkeyPatch, door: str, held_at: str) -> None:
    """A policy door answering 200 for keeping the whole history keeps every version, whichever level it writes.

    PolicyReadBeforeHold: the pass read the table's policy (keep one version) as it started; the owner then sets a hold,
    which answers 200, and the pass, let on, reclaims versions 1 and 2 under the policy it read. Held before its hold, the
    pass reads the policy again once it holds and deletes nothing when it changed (PolicyReread); held inside its cleanup,
    the table's policy door holds the table's history as a ref write does, so it answers 409 beside a reclaim
    (PolicyDoorFence). B1: the table's policy DELETE, handing the table to its namespace's policy keeping every version,
    takes the same hold (PolicyDeleteFence). A namespace's hold binds every table beneath it whatever its own record says
    (HoldAnyLevel, even with no pass running), and the pass holds the policy fence of its namespace and of its project
    while it deletes, so their doors answer 409 beside it (AncestorFence). B4: a namespace door whose process stalls past
    its hold after its take, while a pass is admitted once the hold lapsed, writes only once a renewal shows its hold
    still stands (DoorConfirm). F3: a rename into a namespace that holds binds the table to that hold; it holds the
    table's history as ref work, so it answers 409 beside a reclaim (RenameFence), and a pass that read the chain the
    table left reads it again under its holds and deletes nothing when it changed."""
    if door == "table-delete":
        _ok(estate.catalog.post(f"/management/v1/namespace/{NS}/policy/set", json={"retain_versions": 100}))
    if door == "project-hold":
        _governed_by_a_project(estate)
    if door == "rename-into-a-held-namespace":
        _ok(estate.catalog.post(f"/v1/namespace/{HELD}/create", json={}))
        _ok(estate.catalog.post(f"/management/v1/namespace/{HELD}/policy/set", json={"cleanup_enabled": False}))
    standing = estate.versions()
    path, body = _POLICY_DOORS[door]

    def _door() -> httpx.Response:
        return estate.catalog.post(path, json=body)

    if held_at == "its-door-stalled-past-its-hold":
        answered = _a_door_stalled_past_its_hold(estate, monkeypatch, _door)
    elif held_at == "before-the-pass":
        answered = _door()
        estate.passed()
    elif held_at == "before-its-hold":
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

    kept = estate.versions()
    assert answered.status_code in (200, 409), answered.text
    assert answered.status_code != 200 or kept == standing, f"the {door} answered 200 over versions {standing}, and the pass left {kept}"


def _a_door_stalled_past_its_hold(estate: _Estate, monkeypatch: pytest.MonkeyPatch, door: Callable[[], httpx.Response]) -> httpx.Response:
    """``door`` run in a thread of its own, stalled after its take of a policy fence until the take has lapsed and a pass
    was admitted and reached its cleanup's first delete; the pass is then held there until the door answers."""
    monkeypatch.setattr(policy_fences, "HOLD", timedelta(seconds=2))
    taken, resumed = threading.Event(), threading.Event()
    confirm = fences.Hold.confirm

    def _stalled(hold: fences.Hold) -> None:
        if isinstance(hold._fence, policy_fences.PolicyFence) and hold._work == "set" and not resumed.is_set():  # noqa: SLF001 — which fence and work a hold is of
            taken.set()
            assert resumed.wait(WAIT), "the test never resumed the door"
        confirm(hold)

    monkeypatch.setattr(fences.Hold, "confirm", _stalled)
    answers: list[httpx.Response] = []
    stalled = threading.Thread(target=lambda: answers.append(door()), daemon=True)
    stalled.start()
    assert taken.wait(WAIT), "the door never took its hold"
    time.sleep(3)
    with estate.proxy.holding(_a_cleanup_delete) as held:
        running = _Running(estate)
        assert held.reached(WAIT), "the pass's cleanup deleted nothing"
        resumed.set()
        stalled.join(WAIT)
        held.let_go()
        running.joined()
    (answered,) = answers
    return answered


@pytest.mark.parametrize("door", ["namespace-hold", "table-hold"])
def test_a_hold_beside_a_long_reclaim_lands_after_at_most_one_batch(estate: _Estate, door: str) -> None:
    """F4, R2: a hold asked while a pass reclaims 39 versions beneath it, at the table's namespace or at the table itself,
    answers 409 and leaves an intent; the pass deletes no more than the batch it is in (16 versions,
    ``reclaim_marks.BATCH``) and stops, and the hold asked again lands (PolicyIntent). The intent is what bounds the door:
    a refused hold that left nothing would be refused for as long as passes delete beneath it, while the deletes it was
    asked to stop go on."""
    from lakehouse import reclaim_marks

    estate.written(37)
    path, body = _POLICY_DOORS[door]
    with estate.proxy.holding(_a_cleanup_delete) as held:
        running = _Running(estate)
        assert held.reached(WAIT), "the pass's cleanup deleted nothing"
        first = estate.catalog.post(path, json=body)
        at_first = estate.versions()
        held.let_go()
        running.joined()
    answered = first
    for _ in range(20):
        if answered.status_code == 200:
            break
        time.sleep(0.5)
        answered = estate.catalog.post(path, json=body)
    landed = estate.versions()
    estate.passed()

    assert (first.status_code, answered.status_code) == (409, 200), answered.text
    deleted = len(set(at_first) - set(landed))
    assert deleted <= reclaim_marks.BATCH, f"{deleted} versions were deleted after the hold's first 409"
    assert estate.versions() == landed, "a version was deleted after the hold answered 200"


def _clone_registered(estate: _Estate, version: int) -> tuple[httpx.Response, list[int] | str]:
    """A shallow clone of ``version`` registered through the register door, and what a reader of it reads."""
    location = f"s3://{estate.bucket}/clone"
    estate.opened().shallow_clone(location, version, storage_options=estate.store.options().in_clear())
    answered = estate.catalog.post(f"/v1/table/{NS}$clone/register", json={"location": "clone"})
    return answered, estate.reads(location=location)


@pytest.mark.parametrize(
    ("door", "payload_bytes", "refused", "reclaimer"),
    [
        ("tag", 0, "manifests", "pass"),
        ("restore", 0, "manifests", "pass"),
        ("tag", 5 * 1024 * 1024, "data-files-and-manifests", "pass"),
        ("restore", 5 * 1024 * 1024, "data-files-and-manifests", "pass"),
        ("clone", 5 * 1024 * 1024, "data-files-and-manifests", "pass"),
        ("tag", 5 * 1024 * 1024, "data-files-and-manifests", "maintenance-run-on-main"),
        ("tag", 5 * 1024 * 1024, "data-files-and-manifests", "pass-then-the-policy-raised-and-a-pass"),
    ],
)
def test_a_ref_answered_200_names_a_version_a_reader_can_read_whole(
    estate: _Estate, monkeypatch: pytest.MonkeyPatch, door: str, refused: str, reclaimer: str
) -> None:
    """HalfRemoved: Lance's cleanup deletes the data files of the versions it removes, their blob sidecars, and their
    manifests last (measured on pylance 13.0.0), and the reclaim's credential expires part-way, so the store refuses what
    is left: versions 1 and 2 still stand, over deleted data, or (B2) over deleted sidecars with their data files standing.
    A tag on version 1, a restore of it, or a clone registered on it then answers 200 for a version no reader can read
    whole. The guard (VictimMark): every reclaim marks its victims before its first delete, and a ref door refuses a
    version a standing mark names, whatever files still stand; F1: the catalog's ``maintenance/run?branch=main`` marked
    them as of a ref named ``main``, which no door read as main's. Then (F6, F5) the next pass finishes them below its
    threshold, which a mark makes it do, and the pass that left the mark asked for its table again after
    ``retrySeconds``; R3: the catalog's run whose deletes the store refused answers 503, never ``ok``. R4: a pass that
    finds a dead cleanup's mark once the policy keeps more (``retain_versions`` 100, cleanup still enabled) reclaims
    nothing and must leave the mark (``reclaim_marks._untouched``): the versions it names stand over deleted files, and
    without the mark the door it is followed by names one."""

    def _refused(request: StoreRequest) -> bool:
        return request.deletes("/_versions/") or (refused == "data-files-and-manifests" and request.deletes(".lance"))

    with estate.proxy.answering_expired(_refused):
        if reclaimer == "maintenance-run-on-main":
            outcome = None
            failed = estate.catalog.post(f"/management/v1/table/{T}/maintenance/run?branch=main", json={"retain_versions": 1})
            assert failed.status_code == 503, failed.text
        else:
            outcome = estate.passed()
    assert 1 in estate.versions(), "the reclaim removed version 1 whole, so nothing stands half-removed"
    if reclaimer == "pass-then-the-policy-raised-and-a-pass":
        _ok(estate.catalog.post(f"/management/v1/table/{T}/policy/set", json={"retain_versions": 100}))
        estate.passed()

    if door == "tag":
        answered = estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "kept", "version": 1})
        read = estate.reads("kept")
    elif door == "restore":
        answered = estate.catalog.post(f"/v1/table/{T}/restore", json={"version": 1})
        read = estate.reads()
    else:
        answered, read = _clone_registered(estate, 1)

    assert answered.status_code in (200, 400, 409), answered.text
    assert answered.status_code != 200 or read == [1], f"the {door} answered 200 for version 1, which reads {read}"
    if outcome is not None:
        retry = estate.settings().retry_seconds
        due = outcome.next_due
        assert due is not None and due <= datetime.now(UTC) + timedelta(seconds=retry), f"a pass that left a mark is next due at {due}"
    if reclaimer == "pass-then-the-policy-raised-and-a-pass":
        _ok(estate.catalog.post(f"/management/v1/table/{T}/policy/set", json={"retain_versions": 1}))
    monkeypatch.setenv("MAINTENANCE_MIN_VERSIONS_TO_EXPIRE", "100")
    estate.passed()
    assert (1 in estate.versions(), estate.marks()) == (False, []), "the next pass did not finish the versions a reclaim left marked"


def _planted(estate: _Estate, versions: list[int]) -> str:
    """A mark of ``versions`` of main put directly in the table's history fence directory, as a maintain-tier vend may put
    one, naming no writer."""
    key = f"{history_fences.fence_directory(estate.task.uuid)}/{history_fences.VICTIMS_MARK}.{uuid.uuid4().hex}.json"
    body = f'{{"uuid": "{estate.task.uuid}", "ref": null, "versions": {versions}}}'
    estate.s3().put_object(Bucket=estate.bucket, Key=key, Body=body.encode())
    return key


def _a_reclaim_stalled_past_its_holds(monkeypatch: pytest.MonkeyPatch, *, after_cleanups: int) -> tuple[threading.Event, threading.Event]:
    """Stall the catalog's next reclaim door once it has made ``after_cleanups`` cleanup calls, before it confirms its holds
    for the next, its renewals with it, so its holds (shortened to 2 s) lapse: the event set once it stalled, and the one
    that resumes it."""
    monkeypatch.setattr(history_fences, "HOLD", timedelta(seconds=2))
    monkeypatch.setattr(policy_fences, "HOLD", timedelta(seconds=2))
    stalled, resumed = threading.Event(), threading.Event()
    renew, cleanup, confirm = fences.HolderFence.renew, lance.LanceDataset.cleanup_old_versions, fences.Hold.confirm
    calls = [0]

    def _renew(fence: fences.HolderFence, token: str) -> bool:
        if token.startswith("reclaim-") and stalled.is_set():
            assert resumed.wait(WAIT), "the test never resumed the door"
        return renew(fence, token)

    def _cleanup(dataset: lance.LanceDataset, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — pylance's untyped stats
        done = cleanup(dataset, *args, **kwargs)
        calls[0] += 1
        return done

    def _confirm(hold: fences.Hold) -> None:
        if hold._work == "reclaim" and calls[0] == after_cleanups and not stalled.is_set():  # noqa: SLF001 — which work a hold is for
            stalled.set()
            assert resumed.wait(WAIT), "the test never resumed the door"
        confirm(hold)

    monkeypatch.setattr(fences.HolderFence, "renew", _renew)
    monkeypatch.setattr(lance.LanceDataset, "cleanup_old_versions", _cleanup)
    monkeypatch.setattr(fences.Hold, "confirm", _confirm)
    return stalled, resumed


@pytest.mark.parametrize("mark", ["planted-naming-the-head", "planted-naming-whole-versions", "a-door-whose-hold-lapsed-before-its-first-delete"])
def test_a_mark_makes_a_door_refuse_only_what_it_can_truthfully_say(estate: _Estate, monkeypatch: pytest.MonkeyPatch, mark: str) -> None:
    """F2: a mark is trusted only for what it can truthfully say. One naming a version at or above its ref's head, or one
    a tag names, or more than a batch's worth, is no cleanup's: a door ignores it whole and the next reclaim removes it.
    One naming versions every file of which still stands names nothing half-removed: a reclaim that finds it while its
    writer holds nothing removes it. A door whose hold lapsed before its first delete wrote no mark (it confirms its holds
    before it marks a batch). A mark trusted for more would refuse tags, branches and restores on its versions, the head
    among them, for as long as it stood, and nothing would end it."""
    if mark == "planted-naming-the-head":
        _planted(estate, list(range(1, 50)))
        on_the_head = estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "head", "version": estate.versions()[-1]})
        assert on_the_head.status_code == 200, on_the_head.text
        estate.passed()
    elif mark == "planted-naming-whole-versions":
        _ok(estate.catalog.post(f"/management/v1/table/{T}/policy/set", json={"retain_versions": 100}))
        _planted(estate, [1, 2])
        estate.passed()
    else:
        _stalled, resumed = _a_reclaim_stalled_past_its_holds(monkeypatch, after_cleanups=0)
        threading.Timer(3, resumed.set).start()
        refused = estate.catalog.post(f"/v1/table/{T}/version/delete", json={"ranges": [{"start_version": 1, "end_version": 3}]})
        assert (refused.status_code, estate.versions()) == (409, [1, 2, 3]), refused.text

    tagged = estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "kept", "version": estate.versions()[0]})
    assert (tagged.status_code, estate.marks()) == (200, []), tagged.text


def test_a_ref_door_answers_a_store_that_cannot_say_whether_a_version_is_marked_503(estate: _Estate) -> None:
    """A ref door reads each mark its take of the table's history listed; a store that does not answer the read is a 503
    ServiceUnavailable, never the version's refusal as marked (which tells its caller the version is being reclaimed) and
    never a tag on a version nobody judged."""
    with estate.proxy.answering_expired(lambda request: request.deletes("/_versions/")):
        estate.passed()
    assert estate.marks(), "the pass left no mark"

    with estate.proxy.answering_unavailable(lambda request: request.method == "GET" and f"/{history_fences.VICTIMS_MARK}." in request.path):
        answered = estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "judged", "version": 1})

    assert (answered.status_code, "judged" in estate.opened().tags.list()) == (503, False), answered.text


_RECLAIM_DOORS = {
    "maintenance-run": (f"/management/v1/table/{T}/maintenance/run", {"retain_versions": 1}),
    "version-delete": (f"/v1/table/{T}/version/delete", {"ranges": [{"start_version": 1, "end_version": 20}]}),
}


@pytest.mark.parametrize("door", list(_RECLAIM_DOORS))
@pytest.mark.parametrize("when", ["held-before-the-door", "held-between-its-batches"])
def test_the_catalogs_own_reclaim_doors_delete_nothing_a_hold_keeps(estate: _Estate, monkeypatch: pytest.MonkeyPatch, door: str, when: str) -> None:
    """The catalog's ``maintenance/run`` and ``version/delete`` delete with its own credential, which never dies, and
    ``version/delete`` deleted under a hold ("authorization alone governs it"). A hold at any level that can govern the
    table, here its namespace's over the table's own record keeping one version, refuses both 409 InvalidTableState (19),
    read under the holds every pass takes (``catalog.services.catalog_reclaims``). F6: a door frozen between two of its
    batches past its holds, while the namespace's hold is admitted and answers 200, confirms its holds before its next
    batch, finds them lapsed, and deletes nothing more (409 ConcurrentModification, 14)."""
    hold = f"/management/v1/namespace/{NS}/policy/set"
    path, body = _RECLAIM_DOORS[door]
    estate.written(17)
    if when == "held-before-the-door":
        _ok(estate.catalog.post(hold, json={"cleanup_enabled": False}))
        standing = estate.versions()
        answered = estate.catalog.post(path, json=body)
        assert (answered.status_code, answered.json().get("code"), estate.versions()) == (409, 19, standing), answered.text
        return
    stalled, resumed = _a_reclaim_stalled_past_its_holds(monkeypatch, after_cleanups=1)
    answers: list[httpx.Response] = []
    reclaiming = threading.Thread(target=lambda: answers.append(estate.catalog.post(path, json=body)), daemon=True)
    reclaiming.start()
    assert stalled.wait(WAIT), "the door never came to its second batch"
    time.sleep(2.5)
    held = estate.catalog.post(hold, json={"cleanup_enabled": False})
    at_the_hold = estate.versions()
    resumed.set()
    reclaiming.join(WAIT)
    (answered,) = answers

    assert held.status_code == 200, held.text
    assert (answered.status_code, estate.versions()) == (409, at_the_hold), answered.text


def _ref_door(estate: _Estate, door: str, version: int, *, branch: str | None = None) -> tuple[httpx.Response, list[int] | str]:
    """``door`` (a tag, a restore or a clone's registration) asked of ``version``, and what a reader of what it made reads."""
    if door == "tag":
        answered = estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "kept", "version": version, "branch": branch})
        return answered, estate.reads("kept")
    if door == "restore":
        answered = estate.catalog.post(f"/v1/table/{T}/restore", json={"version": version})
        return answered, estate.reads()
    return _clone_registered(estate, version)


@pytest.mark.parametrize("door", ["tag", "restore"])
def test_an_erasures_fork_point_stands_marked_while_a_branch_keeps_it(estate: _Estate, door: str) -> None:
    """R1: an erasure's reclaim takes the files of the versions its ref's branches were cut from (``Reclaim.run(forks=True)``),
    deepest ref first. Here ``b`` is cut from main v4 (ids 3 and 4, each a fragment of its own) and drops 4, so its head
    stands on main's fragment of 3 alone. Measured on pylance 13.0.0 (ADR 0243 §13): once the branch's own history no longer names the fork's
    files, the cleanup naming the fork deletes them and keeps the fork's manifest for the branch, and the fork version then
    fails every read. Its fork mark stands for as long as a branch is cut from it (it is no stuck reclaim, so the run
    leaves it), and a tag or a restore of it is refused (a shallow clone of it cannot be made: Lance's own clone fails on
    it, measured, so its registration is never asked); once the branch is deleted the mark is handed on as a
    victims mark, and the next pass removes the version and the mark."""
    from catalog.core.config import get_settings
    from catalog.services.erasure import ErasureReport, no_sessions, reclaim_refs
    from lakehouse.history_fences import HistoryWork, TableHistory

    _ok(estate.catalog.post(f"/v1/table/{T}/insert?mode=append", content=_rows([4]), headers=ARROW))
    _ok(estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "b", "from_version": 4}))
    _ok(estate.catalog.post(f"/v1/table/{T}/delete", json={"predicate": "id = 4", "branch": "b"}))
    estate.written(1)
    settings = get_settings()
    so = settings.storage_options()
    history = TableHistory(control_root=settings.registry_root, storage_options=so, table_uuid=estate.task.uuid)
    report = ErasureReport(table=T, predicate="id = 4")
    with history.hold_for(HistoryWork.RECLAIM) as hold:
        reclaim_refs(
            report,
            lance.dataset(estate.task.location, storage_options=so.in_clear()),
            ["b", None],
            clean={"b", None},
            retention=timedelta(0),
            storage_options=so,
            history=history,
            hold=hold,
            sessions=no_sessions,
        )
    assert [surface.outcome for surface in report.surfaces] == ["reclaimed", "reclaimed"], report.surfaces
    assert 4 in estate.versions() and isinstance(estate.reads(4), str), f"the fork version reads {estate.reads(4)}, so nothing of it was taken"

    answered, read = _ref_door(estate, door, 4)
    assert answered.status_code in (200, 400, 409), answered.text
    assert answered.status_code != 200 or read == [3, 4], f"the {door} answered 200 for the fork version, which reads {read}"

    _ok(estate.catalog.post(f"/v1/table/{T}/branches/delete", json={"name": "b"}))
    again, read = _ref_door(estate, door, 4)
    assert again.status_code != 200 or read == [3, 4], f"the {door} answered 200 once the branch went, for a version that reads {read}"
    estate.passed()
    assert (4 in estate.versions(), estate.marks()) == (False, []), "the pass after the branch went did not remove the half-removed fork version"


@pytest.mark.parametrize("ended", ["by-the-delete", "by-the-next-pass"])
def test_a_deleted_branchs_mark_ends_with_it(estate: _Estate, monkeypatch: pytest.MonkeyPatch, ended: str) -> None:
    """R3: a reclaim of branch ``b`` whose manifest deletes the store refused leaves its mark, and the catalog's run says
    so (503, its versions marked for the next run). Deleting ``b`` takes its versions with it, so its mark names nothing a
    door could name: the delete ends it, and where the delete died before ending it, the next reclaim of the table
    removes it, whichever ref that reclaims (a mark names its branch's incarnation, and that incarnation no longer
    stands). A mark that outlived its branch would make every pass vend, age the oldest-mark gauge for ever, and refuse
    the versions of a branch made again under the name."""
    from lakehouse import reclaim_marks

    _ok(estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "b", "from_version": 3}))
    for n in range(3):
        _ok(estate.catalog.post(f"/v1/table/{T}/insert?mode=overwrite&branch=b", content=_rows([200 + n]), headers=ARROW))
    with estate.proxy.answering_expired(lambda request: request.deletes("/_versions/")):
        run = estate.catalog.post(f"/management/v1/table/{T}/maintenance/run?branch=b", json={"retain_versions": 1})
    assert (run.status_code, bool(estate.marks())) == (503, True), run.text

    if ended == "by-the-next-pass":
        monkeypatch.setattr(reclaim_marks, "ended", lambda *_args, **_kwargs: None)
    _ok(estate.catalog.post(f"/v1/table/{T}/branches/delete", json={"name": "b"}))
    if ended == "by-the-next-pass":
        assert estate.marks(), "the delete that died before ending its branch's marks ended them"
        estate.passed()
    assert estate.marks() == [], "a deleted branch's mark outlived its branch"

    _ok(estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "b", "from_version": 3}))
    for n in range(2):
        _ok(estate.catalog.post(f"/v1/table/{T}/insert?mode=overwrite&branch=b", content=_rows([300 + n]), headers=ARROW))
    tagged = estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "again", "version": 4, "branch": "b"})
    assert tagged.status_code == 200, tagged.text


class _MintingVendor:
    """A vendor whose every maintain-tier session is the store's own pair under a key id of its own (moto answers any), so a
    request on the wire names the session that signed it; each mint is written to ``events``."""

    def __init__(self, store: _Store, endpoint: str, events: list[str]) -> None:
        self._store, self._endpoint, self._events, self.minted = store, endpoint, events, 0

    def vend(self, **kwargs: object) -> object:
        from catalog.core.vending import VendedSession

        assert kwargs["tier"] == "maintain" and kwargs["records"] is None, kwargs
        self.minted += 1
        self._events.append("mint")
        key = S3KeyPair(access_key_id=f"batch{self.minted}", secret_access_key=SecretStr(self._store.secret_key))
        return VendedSession(key_pair=key, endpoint=self._endpoint, region="us-east-1", expires_at_millis=int((time.time() + 900) * 1000))

    def vend_reclaim(self, *_args: object, **_kwargs: object) -> None:
        return None


@pytest.mark.parametrize("door", list(_RECLAIM_DOORS))
def test_a_catalog_reclaim_deletes_only_under_a_session_minted_before_its_holds_are_confirmed(
    estate: _Estate, monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    """R5: a catalog door's reclaim deletes under a credential that dies no later than its hold, as a pass's does. Each
    batch first mints a maintain-tier session (STS issues none shorter than the hold), then confirms its holds, renewing
    each for one hold, then deletes under that session alone: so a door frozen inside a call past its hold wakes to a store
    that refuses its deletes. Driven through the real door over 19 versions, two batches, its confirms and Lance's
    cleanup calls written in order beside each mint, and every delete on the wire named by the session that signed it."""
    if estate.store.endpoint == _RUSTFS:
        pytest.skip("the session is the store's own pair under another key id, which only moto answers")
    from catalog.api.dependencies import get_vendor
    from catalog.main import app

    events: list[str] = []
    monkeypatch.setitem(app.dependency_overrides, get_vendor, lambda: _MintingVendor(estate.store, estate.proxy.url, events))
    confirm, cleanup = fences.Hold.confirm, lance.LanceDataset.cleanup_old_versions

    def _confirm(hold: fences.Hold) -> None:
        if hold._work == "reclaim":  # noqa: SLF001 — which work a hold is for
            events.append("confirm")
        confirm(hold)

    def _cleanup(dataset: lance.LanceDataset, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — pylance's untyped stats
        events.append("delete")
        return cleanup(dataset, *args, **kwargs)

    monkeypatch.setattr(fences.Hold, "confirm", _confirm)
    monkeypatch.setattr(lance.LanceDataset, "cleanup_old_versions", _cleanup)
    estate.written(17)
    estate.proxy.taken()
    path, body = _RECLAIM_DOORS[door]
    answered = estate.catalog.post(path, json=body)
    signed = {request.access_key for request in estate.proxy.taken() if _a_cleanup_delete(request)}

    steps = [event for n, event in enumerate(events) if n == 0 or event != events[n - 1]]
    assert (answered.status_code, steps, signed) == (200, ["mint", "confirm", "delete"] * 2, {"batch1", "batch2"}), (answered.text, events, signed)


def test_a_table_no_maintain_vend_could_reach_is_refused_where_it_is_made_and_visible_where_it_stands(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """R6: a pass deletes only under its table's maintain-tier vend, whose session policy names the table's location and
    its bases, and STS takes 2,048 characters of it. A registration at a location whose policy would not fit is refused
    at the register door (400, code 13), naming the limit, before anything is attached; and a table already standing
    whose vend the catalog refuses as too long is held back under a reason of its own
    (``maintenance.reclaims.held_back{reason="policy_too_long"}``), never counted as a missing grant."""
    from maintenance.core import metrics

    deep = "/".join(["deep", *(["d" * 60] * 9)])
    lance.write_dataset(
        pa.table({"id": pa.array([1], pa.int64())}),
        f"s3://{estate.bucket}/{deep}",
        storage_options=estate.store.options().in_clear(),
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    refused = estate.catalog.post(f"/v1/table/{NS}$deep/register", json={"location": deep})
    assert (refused.status_code, refused.json().get("code"), "2,048" in refused.text) == (400, 13, True), refused.text
    described = estate.catalog.post(f"/v1/table/{NS}$deep/describe", json={})
    assert described.status_code == 404, described.text

    held_back: list[str] = []
    monkeypatch.setattr(metrics._reclaims_held_back, "add", lambda amount, attributes, **_: held_back.extend([attributes["reason"]] * amount))
    too_long = {"type": "about:blank", "title": "InvalidInput", "status": 400, "code": 13, "detail": "too long", "session_policy_limit": 2048}
    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=MAINTAIN_TIER_DOOR, params={"tier": "maintain"}).mock(return_value=httpx.Response(400, json=too_long))
        router.route(host="testserver").pass_through()
        outcome = run_table_pass(estate.task, estate.settings())
    assert (held_back, estate.versions()) == (["policy_too_long"], [1, 2, 3]), (held_back, outcome)
