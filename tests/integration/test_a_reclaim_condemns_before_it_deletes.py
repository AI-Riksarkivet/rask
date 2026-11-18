"""A reclaim condemns before it deletes, and a door pins before it reads (ADR 0246, ``specs/maintenance/Reclaim.tla``).

Each test drives one ordering rule or guard of the model through the real maintenance pass (``table_pass.run_table_pass``,
asking the real catalog app for each batch at ``maintenance/condemn``) and the real catalog doors on one S3 store, held
at the step the rule orders: a pass's cleanup on the wire by a proxy in front of the store (Lance's cleanup has no Python
hook), a batch or a door between two of its steps by a pause on the function that step is. The other party runs whole,
the held one is let go, and the claim is judged by what a reader then opens: a door answered 200 names a version a reader
reads whole, a policy answered 200 keeps every version it did not name as condemned, a version whose files went is named
condemned, and a condemned version is finished by the next reclaim. Each runs on moto, and on the pinned RustFS where
``make lifecycle-rustfs-check`` names this file. A cleanup whose credential expired is the store answering its deletes
403 ``ExpiredToken``.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable, Iterator
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

from lakehouse import condemned_versions
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.objectfs import lance_storage_options
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.services.table_pass import PassOutcome, TableTask, run_table_pass
from tests.integration.maintenance_vend_doors import CATALOG_URL, MAINTAIN_TIER_DOOR, MAINTAINER_DOOR, forwarded_to
from tests.integration.store_requests import CountedStore, StoreRequest, counted


NS = "lance_catalog$db"
T = f"{NS}$t"
ARROW = {"Content-Type": ARROW_STREAM_MEDIA_TYPE}
#: How long a step waits for the one it is interleaved with: far above what either takes, so it only bounds a hang.
WAIT = 60
#: The lane's RustFS, where `scripts/lifecycle_on_rustfs_check.sh` stands one up.
_RUSTFS = os.environ.get("RASK_LIFECYCLE_STORE_ENDPOINT", "")
#: Past Lance's dedicated threshold (4 MiB), each row's payload is a blob sidecar of its own beside its data file.
_SIDECAR = 5 * 1024 * 1024


class _Store(BaseModel):
    """An S3 store, and the key pair every party here signs with: the catalog's, the pass's vend's and the reader's."""

    endpoint: str
    access_key: str
    secret_key: str

    def options(self) -> StorageOptions:
        return lance_storage_options(self.endpoint, S3KeyPair(access_key_id=self.access_key, secret_access_key=SecretStr(self.secret_key)), "us-east-1")


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


def _rows(ids: list[int], payload_bytes: int = 0) -> bytes:
    columns: dict[str, pa.Array] = {"id": pa.array(ids, pa.int64())}
    if payload_bytes:
        columns["payload"] = blob_array([os.urandom(payload_bytes) for _ in ids])
    return encode_arrow_stream(pa.table(columns))


def _ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return response.json()


class _Estate(BaseModel):
    """The catalog over a bucket of the test's own, the proxy every party reaches the store through, and the table."""

    model_config = {"arbitrary_types_allowed": True}

    catalog: TestClient
    proxy: CountedStore
    store: _Store
    bucket: str
    task: TableTask
    #: Each row's payload, as the table was made with it.
    payload_bytes: int = 0

    def settings(self) -> MaintenanceSettings:
        from maintenance.core.config import get_settings

        get_settings.cache_clear()
        return get_settings()

    def opened(self, version: int | str | tuple[str | None, int | None] | None = None) -> lance.LanceDataset:
        ds = lance.dataset(self.task.location, storage_options=self.store.options().in_clear())
        return ds if version is None else ds.checkout_version(version)

    def versions(self, branch: str | None = None) -> list[int]:
        ds = self.opened() if branch is None else self.opened((branch, None))
        return [int(entry["version"]) for entry in ds.versions()]

    def written(self, count: int, *, branch: str | None = None) -> None:
        for n in range(count):
            query = "mode=overwrite" + (f"&branch={branch}" if branch else "")
            _ok(self.catalog.post(f"/v1/table/{T}/insert?{query}", content=_rows([100 + n], self.payload_bytes), headers=ARROW))

    def condemned(self) -> list[tuple[str | None, int, str]]:
        """Each entry of the table's condemned versions: its branch's name, its version and its state."""
        from catalog.core.config import get_settings

        settings = get_settings()
        entries, _etag = condemned_versions.read(settings.registry_root, settings.storage_options(), self.task.uuid)
        return sorted(((entry.ref, entry.version, entry.state) for entry in entries), key=lambda found: (found[0] or "", found[1]))

    def s3(self) -> Any:  # noqa: ANN401 — a boto3 S3 client
        return boto3.client(
            "s3",
            endpoint_url=self.store.endpoint,
            aws_access_key_id=self.store.access_key,
            aws_secret_access_key=self.store.secret_key,
            region_name="us-east-1",
        )

    def reads(self, version: int | str | tuple[str | None, int | None] | None = None, *, location: str | None = None) -> list[int] | str:
        """The ids the table (or the dataset at ``location``) holds at ``version``, each payload read whole, or why a
        reader could not read them."""
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
        """The table's pass, run whole: its maintain vend answered with the store's pair over the table's location, and its
        maintainer doors (``maintenance/condemn`` among them) by the real catalog app."""

        def _vend(_request: httpx.Request, table: str) -> httpx.Response:
            vended = {"aws_access_key_id": self.store.access_key, "aws_secret_access_key": self.store.secret_key, "aws_endpoint": "http://store.vended.example"}
            return httpx.Response(200, json={"mode": "direct", "credentials": {"storage_options": vended}, "location": self.task.location})

        with respx.mock(assert_all_called=False) as router:
            router.post(url__regex=MAINTAIN_TIER_DOOR, params={"tier": "maintain"}).mock(side_effect=_vend)
            router.post(url__regex=MAINTAINER_DOOR).mock(side_effect=forwarded_to(self.catalog))
            router.route(host="testserver").pass_through()
            return run_table_pass(self.task, self.settings())


@pytest.fixture
def payload_bytes() -> int:
    return 0


@pytest.fixture
def estate(store: _Store, monkeypatch: pytest.MonkeyPatch, payload_bytes: int) -> Iterator[_Estate]:
    """Table ``T`` at three versions, each an overwrite (ids [1], [2], [3]), under its own policy keeping one version: a
    pass reclaims versions 1 and 2."""
    bucket = f"cond-{uuid.uuid4().hex[:10]}"
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
                yield _Estate(catalog=catalog, proxy=proxy, store=store, bucket=bucket, task=task, payload_bytes=payload_bytes)
        finally:
            app.dependency_overrides.clear()
            get_settings.cache_clear()
            from maintenance.core.config import get_settings as maintenance_settings

            maintenance_settings.cache_clear()


class _Running:
    """``work`` run in a thread of its own, to be held at a step and joined once let go."""

    def __init__(self, work: Callable[[], object]) -> None:
        self.outcome: list[object] = []
        self._thread = threading.Thread(target=self._run, args=(work,), daemon=True)
        self._thread.start()

    def _run(self, work: Callable[[], object]) -> None:
        try:
            self.outcome.append(work())
        except BaseException as exc:  # noqa: BLE001 — joined and raised below
            self.outcome.append(exc)

    def joined(self) -> Any:  # noqa: ANN401 — whatever the work answered
        self._thread.join(timeout=WAIT)
        assert not self._thread.is_alive(), "the work did not end"
        (outcome,) = self.outcome
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Pause:
    """The first call of ``owner.name`` held, before or after it runs: ``reached`` once it got there, ``let_go`` to go on."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, owner: object, name: str, *, after: bool) -> None:
        self.reached, self._let_go, self._taken = threading.Event(), threading.Event(), threading.Lock()
        original = getattr(owner, name)

        def _held(*args: object, **kwargs: object) -> object:
            first = self._taken.acquire(blocking=False)
            if first and not after:
                self._wait()
            answer = original(*args, **kwargs)
            if first and after:
                self._wait()
            return answer

        monkeypatch.setattr(owner, name, _held)

    def _wait(self) -> None:
        self.reached.set()
        assert self._let_go.wait(WAIT), "the test never let the step on"

    def let_go(self) -> None:
        self._let_go.set()


def _a_cleanup_delete(request: StoreRequest) -> bool:
    """A delete Lance's cleanup sends: of a transaction file (its first, measured), a data file or a manifest."""
    return any(request.deletes(directory) for directory in ("/_transactions/", "/data/", "/_versions/"))


def _a_manifest_delete(request: StoreRequest) -> bool:
    """A final manifest's delete, the last Lance's cleanup sends of a version (measured on pylance 13.0.0), never a staged
    one's a door's commit discards."""
    return request.deletes(".manifest</Key>") or (request.method == "DELETE" and "/_versions/" in request.path and request.path.endswith(".manifest"))


def _standing_condemned(estate: _Estate) -> list[tuple[str | None, int, str]]:
    """The entries naming a version whose manifest stands: an entry of one gone is dropped by the next rewrite."""
    return [(ref, version, state) for ref, version, state in estate.condemned() if version in estate.versions(ref)]


def _clone_location(estate: _Estate) -> str:
    return f"s3://{estate.bucket}/clone"


def _ref_door(estate: _Estate, door: str, version: int, *, branch: str | None = None) -> httpx.Response:
    """``door`` (a tag, a restore, a branch create or a clone's registration) asked of ``version``."""
    if door == "tag":
        return estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "kept", "version": version, "branch": branch})
    if door == "restore":
        return estate.catalog.post(f"/v1/table/{T}/restore", json={"version": version})
    if door == "branch":
        return estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "cut", "from_version": version})
    estate.opened().shallow_clone(_clone_location(estate), version, storage_options=estate.store.options().in_clear())
    return estate.catalog.post(f"/v1/table/{NS}$clone/register", json={"location": "clone"})


def _made(estate: _Estate, door: str) -> list[int] | str:
    """What a reader of what ``door`` made reads now."""
    if door == "tag":
        return estate.reads("kept")
    if door == "restore":
        return estate.reads()
    if door == "branch":
        return estate.reads(("cut", None))
    return estate.reads(location=_clone_location(estate))


@pytest.mark.parametrize("payload_bytes", [_SIDECAR])
@pytest.mark.parametrize("door", ["tag", "restore", "branch", "clone"])
def test_a_ref_answered_200_beside_a_reclaims_deletes_names_a_whole_version(estate: _Estate, door: str) -> None:
    """CondemnFirst: the pass's batch condemned versions 1 and 2 before its cleanup's first delete, held here; a door on
    version 1 pins it, reads the record and is refused (409, code 19). A batch that condemned only after its cleanup
    would have let the door's ref land on a version the cleanup goes on to delete."""
    with estate.proxy.holding(_a_cleanup_delete) as held:
        running = _Running(estate.passed)
        assert held.reached(WAIT), "the pass's cleanup deleted nothing"
        answered = _ref_door(estate, door, 1)
        held.let_go()
        running.joined()
    read = _made(estate, door)

    assert answered.status_code in (200, 400, 409), answered.text
    assert answered.status_code != 200 or read == [1], f"the {door} answered 200 for version 1, which reads {read}"
    assert answered.status_code != 409 or answered.json()["code"] == 19, answered.text


@pytest.mark.parametrize("payload_bytes", [_SIDECAR])
@pytest.mark.parametrize("door", ["tag", "branch"])
def test_a_door_that_read_the_record_before_a_batch_condemned_keeps_its_version(estate: _Estate, monkeypatch: pytest.MonkeyPatch, door: str) -> None:
    """PinFirst: a door that pinned version 1 and read no entry naming it is held before it acts; the pass runs and is
    held at its first manifest delete, its data deletes done; the door then acts. Its pin was standing at the batch's pin
    read, so the batch excluded version 1 and its ref names a version a reader reads whole. A door that read the record
    before it pinned would have pinned after that read, and its 200 would name a version the cleanup has half deleted."""
    door_read = _Pause(monkeypatch, condemned_versions, "condemned", after=True)
    answers = _Running(lambda: _ref_door(estate, door, 1))
    assert door_read.reached.wait(WAIT), "the door never read the record"
    with estate.proxy.holding(_a_manifest_delete) as held:
        running = _Running(estate.passed)
        assert held.reached(WAIT), "the pass deleted no manifest"
        door_read.let_go()
        answered = answers.joined()
        held.let_go()
        running.joined()

    assert answered.status_code in (200, 409), answered.text
    assert answered.status_code != 200 or _made(estate, door) == [1], f"the {door} answered 200 for version 1, which reads {_made(estate, door)}"


@pytest.mark.parametrize("payload_bytes", [_SIDECAR])
def test_a_version_whose_deletes_the_store_refused_stays_condemned_until_the_next_reclaim_finishes_it(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """KeepAfterDelete, GCOnlyGone, and a dead batch finished by the next: the pass's cleanup has its data files and
    manifests refused (its vend expired), so versions 1 and 2 stand over deleted files. Their entries stand: a tag on
    version 1 is refused, and so it is after another reclaim of the table rewrote the record (a branch's), which drops only
    entries whose manifests are gone. The next pass, below its threshold, finishes them, and the record is left empty."""
    _ok(estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "b", "from_version": 3}))
    estate.written(2, branch="b")
    with estate.proxy.answering_expired(lambda request: request.deletes("/_versions/") or request.deletes(".lance") or request.deletes(".blob")):
        estate.passed()
    assert {1, 2} <= set(estate.versions()), "the reclaim removed the versions whole, so nothing stands half removed"
    assert {(None, 1, "g"), (None, 2, "g")} <= set(estate.condemned()), estate.condemned()
    refused = estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "kept", "version": 1})
    assert (refused.status_code, refused.json().get("code")) == (409, 19), refused.text

    _ok(estate.catalog.post(f"/management/v1/table/{T}/maintenance/run?branch=b", json={"retain_versions": 1}))
    again = estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "kept", "version": 1})
    assert again.status_code == 409, again.text

    monkeypatch.setenv("MAINTENANCE_MIN_VERSIONS_TO_EXPIRE", "100")
    estate.passed()
    assert (set(estate.versions()) & {1, 2}, estate.condemned()) == (set(), []), "the next pass did not finish what the refused one left"


def test_a_dead_doors_pin_on_a_go_version_is_deleted_and_the_version_finished(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """GoUnpins: a batch promoted versions 1 and 2 and its cleanup's manifest deletes were refused; a door then pinned
    version 1 and died before it read or unpinned (its pin is left). The next batch finds the pin on a version another
    batch named ``g``: no door that pinned it since can act on it (its record read refuses it), so the batch deletes the
    pin and finishes the version. Left, the pin would keep a version half removed for good: Lance keeps every tagged
    version."""
    with estate.proxy.answering_expired(_a_manifest_delete):
        estate.passed()
    pinned = condemned_versions.pin(estate.opened(), None, 1)
    assert pinned in estate.opened().tags.list()

    monkeypatch.setenv("MAINTENANCE_MIN_VERSIONS_TO_EXPIRE", "100")
    estate.passed()
    assert (1 in estate.versions(), pinned in estate.opened().tags.list()) == (False, False), "the dead door's pin kept a version half removed"


def test_a_dead_doors_pin_past_its_lifetime_is_swept(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """A3': a door that died after pinning version 1, a version no batch condemned, leaves its pin, which keeps the
    version whole; once older than a door can live (``PIN_LIFETIME``, here none) a batch deletes it, counted, and the
    version is then reclaimed as any other."""
    _ok(estate.catalog.post(f"/management/v1/table/{T}/policy/set", json={"retain_versions": 100}))
    pinned = condemned_versions.pin(estate.opened(), None, 1)
    _ok(estate.catalog.post(f"/management/v1/table/{T}/policy/set", json={"retain_versions": 1}))
    estate.passed()
    assert (1 in estate.versions(), pinned in estate.opened().tags.list()) == (True, True), "a live door's pin was swept"

    monkeypatch.setattr(condemned_versions, "PIN_LIFETIME", condemned_versions.PIN_LIFETIME * 0)
    estate.passed()
    estate.passed()
    assert (1 in estate.versions(), pinned in estate.opened().tags.list()) == (False, False), "a dead door's pin kept its version for good"


def test_a_caller_neither_plants_nor_lifts_a_pin(estate: _Estate) -> None:
    """A pin keeps its version from every reclaim, so only the catalog's own doors write one (B2): a caller's tag named as
    a pin is refused (400, code 13), and a door's standing pin is no tag a caller can read, move, delete or list."""
    pinned = condemned_versions.pin(estate.opened(), None, 1)

    planted = estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": condemned_versions.pin_name(), "version": 2})
    asked = {
        verb: estate.catalog.post(f"/v1/table/{T}/tags/{verb}", json={"tag": pinned, **({"version": 2} if verb == "update" else {})}).status_code
        for verb in ("version", "update", "delete")
    }
    listed = _ok(estate.catalog.post(f"/v1/table/{T}/tags/list", json={}))["tags"]

    assert (planted.status_code, planted.json().get("code")) == (400, 13), planted.text
    assert (asked, pinned in listed) == ({"version": 404, "update": 404, "delete": 404}, False), (asked, listed)
    assert estate.opened().tags.list()[pinned]["version"] == 1, "a caller moved or lifted the catalog's pin"


#: The namespace a rename moves the table into, whose hold then binds it.
HELD = "lance_catalog$held"
#: The project a case gives the bucket to, so its policy may govern the table.
PROJECT = "acme"

_POLICY_DOORS = {
    "table-hold": (f"/management/v1/table/{T}/policy/set", {"cleanup_enabled": False}),
    "namespace-hold": (f"/management/v1/namespace/{NS}/policy/set", {"cleanup_enabled": False}),
    "project-hold": (f"/management/v1/project/{PROJECT}/policy/set", {"cleanup_enabled": False}),
    "rename-into-a-held-namespace": (f"/v1/table/{T}/rename", {"new_namespace_id": HELD.split("$"), "new_table_name": "t"}),
}


def _governed_by_a_project(estate: _Estate) -> None:
    """The bucket made the one warehouse of project ``PROJECT``, as the catalog records a warehouse's bucket."""
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


def _readied(estate: _Estate, door: str) -> None:
    if door == "project-hold":
        _governed_by_a_project(estate)
    if door == "rename-into-a-held-namespace":
        _ok(estate.catalog.post(f"/v1/namespace/{HELD}/create", json={}))
        _ok(estate.catalog.post(f"/management/v1/namespace/{HELD}/policy/set", json={"cleanup_enabled": False}))


@pytest.mark.parametrize(
    ("door", "batch_held"),
    [
        ("table-hold", "after-its-policy-read"),
        ("namespace-hold", "after-its-policy-read"),
        ("project-hold", "after-its-policy-read"),
        ("namespace-hold", "before-its-policy-read"),
        ("rename-into-a-held-namespace", "before-its-policy-read"),
    ],
)
def test_a_hold_answered_beside_a_reclaim_keeps_every_version_it_did_not_name(
    estate: _Estate, monkeypatch: pytest.MonkeyPatch, door: str, batch_held: str
) -> None:
    """PolicyAfterCondemn: a batch condemns, then reads the policy, level by level, on the chain its table's claim names
    then. A hold asked while the batch is held answers 200 at once and names, machine-readable, the versions already
    condemned (``condemned``); every other version stands whole after the pass, and nothing it named is promised. Held
    before its policy read, the batch reads the hold (a rename's included: the chain is read after the condemn) and
    deletes nothing. A batch that read the policy before it condemned would delete under a hold whose read named
    nothing."""
    from catalog.services import reclaims

    _readied(estate, door)
    batch = _Pause(monkeypatch, reclaims.Governance, "held_by", after=batch_held == "after-its-policy-read")
    standing = estate.versions()
    path, body = _POLICY_DOORS[door]
    running = _Running(estate.passed)
    assert batch.reached.wait(WAIT), "the pass's batch never read the policy"
    answered = estate.catalog.post(path, json=body)
    batch.let_go()
    running.joined()

    assert answered.status_code == 200, answered.text
    named = {(entry.get("branch"), entry["version"]) for entry in answered.json().get("condemned", [])}
    if door == "rename-into-a-held-namespace":
        assert estate.versions() == standing, f"a rename into a held namespace beside a reclaim left {estate.versions()} of {standing}"
        return
    if batch_held == "after-its-policy-read":
        assert named == {(None, 1), (None, 2)}, answered.text
    kept = [version for version in standing if (None, version) not in named]
    assert all(version in estate.versions() and isinstance(estate.reads(version), list) for version in kept), (
        f"the hold answered {named} and left {estate.versions()}"
    )


def test_a_hold_door_writes_before_it_reads(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """HoldWritesFirst: a namespace's hold is held between its write and its read of the condemned versions; the pass runs
    whole meanwhile, reads the hold after its condemn and deletes nothing, and the door then answers naming nothing.
    A door that read first would name nothing, then write after a batch's policy read, and the batch would delete what
    it promised."""
    from catalog.api.v1.endpoints import policies

    standing = estate.versions()
    door = _Pause(monkeypatch, policies, "_condemned_under", after=False)
    answers = _Running(lambda: estate.catalog.post(f"/management/v1/namespace/{NS}/policy/set", json={"cleanup_enabled": False}))
    assert door.reached.wait(WAIT), "the door never came to its read"
    estate.passed()
    door.let_go()
    answered = answers.joined()

    assert (answered.status_code, answered.json().get("condemned")) == (200, []), answered.text
    assert estate.versions() == standing, f"the hold answered naming nothing, and the pass left {estate.versions()} of {standing}"


def test_version_delete_condemns_and_a_hold_keeps_what_it_condemned(estate: _Estate) -> None:
    """``version/delete`` is the spec's (``BatchDeleteTableVersions`` deletes version records, not data): it condemns the
    versions it names, which ``version/list`` then leaves out and every ref door refuses, and deletes nothing. Under a
    namespace's hold the next pass keeps them whole; once the hold is lifted the next pass finishes them."""
    estate.written(2)
    _ok(estate.catalog.post(f"/management/v1/namespace/{NS}/policy/set", json={"cleanup_enabled": False}))
    deleted = _ok(estate.catalog.post(f"/v1/table/{T}/version/delete", json={"ranges": [{"start_version": 2, "end_version": 4}]}))
    listed = [row["version"] for row in _ok(estate.catalog.post(f"/v1/table/{T}/version/list", json={}))["versions"]]
    refused = estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "kept", "version": 3})
    estate.passed()

    assert (deleted["deleted_count"], listed, refused.status_code) == (2, [1, 4, 5], 409), (deleted, listed, refused.text)
    assert {2, 3} <= set(estate.versions()) and estate.reads(3) == [3], "a version condemned under a hold was deleted"
    lifted = _ok(estate.catalog.post(f"/management/v1/namespace/{NS}/policy/delete", json={}))
    assert {(entry.get("branch"), entry["version"]) for entry in lifted["condemned"]} == {(None, 2), (None, 3)}, lifted
    estate.passed()
    assert set(estate.versions()) & {2, 3} == set(), "the pass after the hold was lifted did not finish what version/delete condemned"


def test_entries_are_keyed_by_the_branchs_incarnation(estate: _Estate) -> None:
    """IdentityKeys: a reclaim of branch ``b`` whose manifest deletes the store refused leaves its versions condemned;
    ``b`` is deleted and made again under its name, another incarnation whose versions repeat the numbers. Its versions
    are no condemned version's: a tag on one lands and reads whole. Entries keyed by the branch's name would refuse it,
    and finish a version nobody chose."""
    _ok(estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "b", "from_version": 3}))
    estate.written(3, branch="b")
    with estate.proxy.answering_expired(_a_manifest_delete):
        run = estate.catalog.post(f"/management/v1/table/{T}/maintenance/run?branch=b", json={"retain_versions": 1})
    assert run.status_code == 503 and any(ref == "b" for ref, _version, _state in estate.condemned()), run.text

    _ok(estate.catalog.post(f"/v1/table/{T}/branches/delete", json={"name": "b"}))
    _ok(estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "b", "from_version": 3}))
    estate.written(2, branch="b")
    tagged = estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "again", "version": 4, "branch": "b"})
    assert (tagged.status_code, estate.reads("again")) == (200, [100]), tagged.text
    estate.passed()
    assert all(ref != "b" or version not in (4, 5) for ref, version, _state in _standing_condemned(estate)), (
        "a deleted incarnation's entries refused the new one's versions"
    )


@pytest.mark.parametrize("door", ["tag", "restore"])
def test_an_erasures_fork_point_stays_condemned_while_a_branch_keeps_it(estate: _Estate, door: str) -> None:
    """An erasure's reclaim takes the files of the versions its ref's branches were cut from, deepest ref first. Here
    ``b`` is cut from main v4 (ids 3 and 4, a fragment each) and drops 4, so its head stands on main's fragment of 3
    alone; once the branch's history no longer names the fork's files the cleanup naming the fork deletes them and keeps
    its manifest for the branch (measured on pylance 13.0.0, ADR 0243 §13). Its entry stands while a branch is cut from
    it: a tag or a restore of it is refused. Once the branch is deleted the next pass finishes the version."""
    from catalog.core.config import get_settings
    from catalog.services.erasure import ErasureReport, reclaim_refs

    _ok(estate.catalog.post(f"/v1/table/{T}/insert?mode=append", content=_rows([4]), headers=ARROW))
    _ok(estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "b", "from_version": 4}))
    _ok(estate.catalog.post(f"/v1/table/{T}/delete", json={"predicate": "id = 4", "branch": "b"}))
    estate.written(1)
    settings = get_settings()
    so = settings.storage_options()
    main = lance.dataset(estate.task.location, storage_options=so.in_clear())

    def _condemn(ref: str | None) -> Callable[[list[int]], condemned_versions.Verdict]:
        def _batch(versions: list[int]) -> condemned_versions.Verdict:
            run = condemned_versions.Batch(settings.registry_root, so, table_uuid=estate.task.uuid, location=estate.task.location, dataset=main, ref=ref)
            run.condemn(versions, forks=True)
            run.read_pins()
            return run.promote(None)

        return _batch

    report = ErasureReport(table=T, predicate="id = 4")
    reclaim_refs(report, main, ["b", None], clean={"b", None}, retention=condemned_versions.PIN_LIFETIME * 0, storage_options=so, condemn=_condemn)
    assert [surface.outcome for surface in report.surfaces] == ["reclaimed", "reclaimed"], report.surfaces
    assert 4 in estate.versions() and isinstance(estate.reads(4), str), f"the fork version reads {estate.reads(4)}, so nothing of it was taken"

    answered = _ref_door(estate, door, 4)
    read = _made(estate, door)
    assert answered.status_code == 409 or read == [3, 4], f"the {door} answered {answered.status_code} for the fork version, which reads {read}"
    _ok(estate.catalog.post(f"/v1/table/{T}/branches/delete", json={"name": "b"}))
    estate.passed()
    assert (4 in estate.versions(), _standing_condemned(estate)) == (False, []), "the pass after the branch went did not finish the half-removed fork version"
