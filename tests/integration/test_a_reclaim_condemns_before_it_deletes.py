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

from lakehouse import condemned_versions
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.objectfs import lance_storage_options
from lakehouse.storage_options import StorageOptions
from lakehouse.version_retention import branch_identity
from maintenance.core.config import MaintenanceSettings
from maintenance.services.table_pass import PassOutcome, TableTask, run_table_pass
from tests.integration.maintenance_vend_doors import CATALOG_URL, MAINTAIN_TIER_DOOR, MAINTAINER_DOOR, forwarded_to
from tests.integration.store_requests import CountedStore, HeldRequests, StoreRequest, counted


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

    def licence_on(self, name: str) -> datetime | None:
        """When the last live licence under the branch name ``name`` ends, by the table's record."""
        from catalog.core.config import get_settings

        settings = get_settings()
        return condemned_versions.licensed_under(settings.registry_root, settings.storage_options(), table_uuid=self.task.uuid, name=name)

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

    def passed(self, *, vend_lives: timedelta = condemned_versions.CREDENTIAL_LIFETIME) -> PassOutcome:
        """The table's pass, run whole: its maintain vend answered with the store's pair over the table's location,
        stamped to expire ``vend_lives`` from its answer as the catalog's door stamps a session, and its maintainer doors
        (``maintenance/condemn`` among them) by the real catalog app."""

        def _vend(_request: httpx.Request, table: str) -> httpx.Response:
            vended = {"aws_access_key_id": self.store.access_key, "aws_secret_access_key": self.store.secret_key, "aws_endpoint": "http://store.vended.example"}
            expires = int((datetime.now(UTC) + vend_lives).timestamp() * 1000)
            credentials = {"storage_options": vended, "expires_at_millis": expires}
            return httpx.Response(200, json={"mode": "direct", "credentials": credentials, "location": self.task.location})

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

    def held_at(self, held: HeldRequests) -> bool:
        """Whether the work reached ``held``'s request, or ended without reaching it, within the suite's wait."""
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if held.reached(0.2):
                return True
            if not self._thread.is_alive():
                return False
        raise AssertionError("the work neither reached the held request nor ended")

    def joined(self) -> Any:  # noqa: ANN401 — whatever the work answered
        self._thread.join(timeout=WAIT)
        assert not self._thread.is_alive(), "the work did not end"
        (outcome,) = self.outcome
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Pause:
    """The first call of ``owner.name`` (that ``when`` takes) held, before or after it runs: ``reached`` once it got there,
    ``let_go`` to go on."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, owner: object, name: str, *, after: bool, when: Callable[..., bool] = lambda *_args, **_kwargs: True
    ) -> None:
        self.reached, self._let_go, self._taken = threading.Event(), threading.Event(), threading.Lock()
        original = getattr(owner, name)

        def _held(*args: object, **kwargs: object) -> object:
            first = when(*args, **kwargs) and self._taken.acquire(blocking=False)
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


#: Each kind of pin the catalog writes: a door's, for the length of its request, and an erasure release's on where its
#: branches come back, kept for as long as the release runs however long its tick stalls.
_PINS = {"door": condemned_versions.pin_name, "release": lambda: condemned_versions.release_pin_name(uuid.uuid4().hex, 1)}


@pytest.mark.parametrize("kind", list(_PINS))
def test_a_dead_doors_pin_past_its_lifetime_is_swept(estate: _Estate, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """A3': a door that died after pinning version 1, a version no batch condemned, leaves its pin, which keeps the
    version whole; once older than a door can live (``PIN_LIFETIME``, here none) a batch deletes it, counted, and the
    version is then reclaimed as any other. ReleasePinKind: an erasure release's pin is never a dead door's, so no
    batch sweeps it whatever its age; a sweep that took it would let a reclaim take where a stalled release makes its
    branches again (``ReclaimReleaseSwept.cfg``)."""
    _ok(estate.catalog.post(f"/management/v1/table/{T}/policy/set", json={"retain_versions": 100}))
    pinned = condemned_versions.pin(estate.opened(), None, 1, name=_PINS[kind]())
    _ok(estate.catalog.post(f"/management/v1/table/{T}/policy/set", json={"retain_versions": 1}))
    estate.passed()
    assert (1 in estate.versions(), pinned in estate.opened().tags.list()) == (True, True), "a live door's pin was swept"

    monkeypatch.setattr(condemned_versions, "PIN_LIFETIME", condemned_versions.PIN_LIFETIME * 0)
    estate.passed()
    estate.passed()
    swept = kind == "door"
    assert (1 in estate.versions(), pinned in estate.opened().tags.list()) == (not swept, not swept), f"a {kind}'s pin past the lifetime"


@pytest.mark.parametrize("kind", list(_PINS))
def test_a_caller_neither_plants_nor_lifts_a_pin(estate: _Estate, kind: str) -> None:
    """A pin keeps its version from every reclaim, so only the catalog writes one (B2): a caller's tag named under the
    prefix the catalog keeps for its own tags (``rask-``) is refused (400, code 13), and a standing pin of either kind is
    no tag a caller can read, move, delete or list."""
    pinned = condemned_versions.pin(estate.opened(), None, 1, name=_PINS[kind]())

    planted = {name: estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": name, "version": 2}) for name in (_PINS[kind](), "rask-anything")}
    asked = {
        verb: estate.catalog.post(f"/v1/table/{T}/tags/{verb}", json={"tag": pinned, **({"version": 2} if verb == "update" else {})}).status_code
        for verb in ("version", "update", "delete")
    }
    listed = _ok(estate.catalog.post(f"/v1/table/{T}/tags/list", json={}))["tags"]

    assert {name: (answer.status_code, answer.json().get("code")) for name, answer in planted.items()} == dict.fromkeys(planted, (400, 13)), planted
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


def test_entries_are_keyed_by_the_branchs_incarnation(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """IdentityKeys: a reclaim of branch ``b`` whose manifest deletes the store refused leaves its versions condemned;
    ``b`` is deleted and, its licence ended (here at once), made again under its name, another incarnation whose versions
    repeat the numbers. Its versions are no condemned version's: a tag on one lands and reads whole. Entries keyed by the
    branch's name would refuse it, and finish a version nobody chose."""
    monkeypatch.setattr(condemned_versions, "LICENCE", condemned_versions.LICENCE * 0)
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

    def _condemn(ref: str | None) -> condemned_versions.RefReclaim:
        listed_on = condemned_versions.identity_of(main, ref, table_uuid=estate.task.uuid)

        def _batch(versions: list[int]) -> condemned_versions.Verdict:
            run = condemned_versions.Batch(
                settings.registry_root, so, table_uuid=estate.task.uuid, location=estate.task.location, dataset=main, ref=ref, listed_on=listed_on
            )
            run.condemn(versions, forks=True)
            run.read_pins()
            return run.promote(None)

        handle = main if ref is None else main.checkout_version((ref, None))
        return condemned_versions.RefReclaim(condemn=_batch, mint=lambda: condemned_versions.Credential(dataset=handle, expires_at=None))

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


#: A ref door asked with no version: a branch cut from main or from a branch, and the doors whose request must name one.
_UNVERSIONED = {
    "branch-from-main": ("/branches/create", {"name": "cut"}),
    "branch-from-a-branch": ("/branches/create", {"name": "cut", "from_branch": "b"}),
    "tag": ("/tags/create", {"tag": "kept"}),
    "tag-update": ("/tags/update", {"tag": "kept"}),
    "restore": ("/restore", {}),
}


@pytest.mark.parametrize("payload_bytes", [_SIDECAR])
@pytest.mark.parametrize("door", list(_UNVERSIONED))
def test_a_door_asked_with_no_version_acts_on_the_number_it_pinned(estate: _Estate, monkeypatch: pytest.MonkeyPatch, door: str) -> None:
    """No ref door acts on an implicit version (ADR 0246 §14, V1). The door's handle is opened as its request starts, and
    it is held once it pinned and read the record; the ref then moves on (an insert) and a reclaim of it condemns, licenses
    and deletes what it no longer keeps (``maintenance/run``, ``retain_versions: 1``), held at its manifest delete; the door
    then acts, and the reclaim goes on. Asked with no
    version, a branch create
    reads the source's head fresh, pins that number and hands it to Lance, so its pin kept the version (or it was refused)
    and the branch reads whole. One that handed Lance no version cut at its handle's version, which no pin kept: 200, and
    a branch no reader opens. The tag and restore doors' requests name a version, or are refused."""
    from catalog.services import dataplane

    if door == "branch-from-a-branch":
        _ok(estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "b", "from_version": 3}))
        estate.written(1, branch="b")
    path, body = _UNVERSIONED[door]
    if door in ("tag", "tag-update", "restore"):
        refused = estate.catalog.post(f"/v1/table/{T}{path}", json=body)
        assert (refused.status_code, estate.opened().tags.list(), estate.versions()) == (400, {}, [1, 2, 3]), refused.text
        return
    door_read = _Pause(monkeypatch, dataplane, "require_whole", after=True)
    answers = _Running(lambda: estate.catalog.post(f"/v1/table/{T}{path}", json=body))
    assert door_read.reached.wait(WAIT), "the door never pinned"
    on = "?branch=b" if door == "branch-from-a-branch" else ""
    estate.written(1, branch="b" if on else None)
    with estate.proxy.holding(_a_manifest_delete) as held:
        reclaim = _Running(lambda: estate.catalog.post(f"/management/v1/table/{T}/maintenance/run{on}", json={"retain_versions": 1}))
        reclaim.held_at(held)
        door_read.let_go()
        answered = answers.joined()
        held.let_go()
        reclaim.joined()
    read = estate.reads(("cut", None))

    assert answered.status_code in (200, 409), answered.text
    assert answered.status_code != 200 or isinstance(read, list), f"the branch answered 200 and reads {read}"


def _held_on_b(estate: _Estate, monkeypatch: pytest.MonkeyPatch, *, at: str) -> tuple[_Running, _Pause]:
    """Branch ``b`` cut from main 3 with two versions of its own (4, 5), and a reclaim of it (``maintenance/run``,
    ``retain_versions: 1``) held ``at`` a step of its batch of versions 3 and 4: once promoted, before its cleanup (a
    reclaimer paused with a live licence, its handle on the incarnation it listed), or once it read the pins, before its
    promotion."""
    _ok(estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "b", "from_version": 3}))
    estate.written(2, branch="b")
    step = _Pause(monkeypatch, condemned_versions.Batch, "promote" if at == "promoted" else "read_pins", after=True)
    running = _Running(lambda: estate.catalog.post(f"/management/v1/table/{T}/maintenance/run?branch=b", json={"retain_versions": 1}))
    assert step.reached.wait(WAIT), f"the reclaim of b never reached its batch's step ({at})"
    return running, step


def _the_first_listing_of_b() -> Callable[[StoreRequest], bool]:
    """The first listing of branch ``b``'s manifests from now on, and no later one: the held cleanup's, after its read of
    the tags (measured on pylance 13.0.0), which a door's own listing then passes."""
    taken = threading.Lock()

    def first(request: StoreRequest) -> bool:
        listing = request.method == "GET" and "list-type" in request.query and request.query.get("prefix", [""])[0].endswith("tree/b/_versions/")
        return listing and taken.acquire(blocking=False)

    return first


#: How ``b`` is made again beside a reclaim of the incarnation deleted, and what then promises one of its versions.
_MADE_AGAIN = {
    # The review's (b): a reclaim promoted and paused; a tag answered 200 on the new incarnation once its cleanup read the tags.
    "tagged": ("promoted", False),
    # The review's (a): the same with the table's hold answered 200, naming no version of the new incarnation.
    "held": ("promoted", False),
    # GCAfterLicence: another reclaim rewrites the record between the delete and the create.
    "tagged-after-a-rewrite": ("promoted", True),
    # StandsAfterGo: the reclaim promotes only once the delete and the create landed, the create having read no licence.
    "tagged-before-its-promotion": ("read the pins", False),
}


@pytest.mark.parametrize("payload_bytes", [_SIDECAR])
@pytest.mark.parametrize("made_again", list(_MADE_AGAIN))
def test_a_branch_made_again_under_its_name_loses_nothing_to_a_reclaim_of_the_one_deleted(
    estate: _Estate, monkeypatch: pytest.MonkeyPatch, made_again: str
) -> None:
    """A licence names numbers under a branch's name (ADR 0246 §14, V2, the review's interleavings). A reclaim of ``b``
    batches versions 3 and 4; ``b`` is deleted (never refused for a reclaim) and made again from main 3, a new
    incarnation repeating the numbers, under which a tag or the table's hold then promises version 4 or more; the reclaim
    goes on. NameGuard: while a licence under the name lives, the create is refused (409, code 14, naming the instant),
    and once it has passed (here 20 s) the name is made again and reads whole. GCAfterLicence: a rewrite of the record
    meanwhile keeps the deleted incarnation's licence. StandsAfterGo: a batch promoted only after the create read no
    licence reads the branches and licenses nothing. Without any one, the reclaim's cleanup deletes by number under the
    name what the tag or the hold promised."""
    at, rewritten = _MADE_AGAIN[made_again]
    monkeypatch.setattr(condemned_versions, "LICENCE", timedelta(seconds=20))
    running, step = _held_on_b(estate, monkeypatch, at=at)
    _ok(estate.catalog.post(f"/v1/table/{T}/branches/delete", json={"name": "b"}))
    if rewritten:
        _ok(estate.catalog.post(f"/management/v1/table/{T}/maintenance/run", json={"retain_versions": 1}))
    again = estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "b", "from_version": 3})
    promised: set[int] = set()
    if again.status_code == 200:
        estate.written(2, branch="b")
        if made_again == "held":
            answer = _ok(estate.catalog.post(f"/management/v1/table/{T}/policy/set", json={"cleanup_enabled": False}))
            named = {(entry["branch_identifier"], entry["version"]) for entry in answer["condemned"] if entry.get("branch") == "b"}
            identity = branch_identity(estate.opened().branches.list()["b"])
            promised = {version for version in estate.versions("b") if (identity, version) not in named}
    with estate.proxy.holding(_the_first_listing_of_b()) as held:
        step.let_go()
        listed = running.held_at(held)
        if listed and again.status_code == 200 and made_again != "held":
            _ok(estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "kept", "version": 4, "branch": "b"}))
            promised = {4}
        held.let_go()
        running.joined()

    assert again.status_code == 409 or all(isinstance(estate.reads(("b", version)), list) for version in promised), (
        f"b made again answered {again.status_code}; of the versions promised {promised} b now holds {estate.versions('b')}"
    )
    if at == "promoted":
        assert (again.status_code, again.json().get("code")) == (409, 14) and "until" in again.json()["detail"], again.text
        ends = estate.licence_on("b")
        time.sleep(0.0 if ends is None else max(0.0, ends.timestamp() - time.time()) + 0.5)
        _ok(estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "b", "from_version": 3}))
        estate.written(2, branch="b")
    assert estate.reads(("b", 4)) == [100], "b made again does not read whole"


def _registered(estate: _Estate) -> _Estate:
    """The estate's table ``r`` instead: written at ``reg`` with three versions (ids [1], [2], [3]), registered there under a
    policy keeping one. A location of the writer's own, so it may be deregistered and registered again, another uuid."""
    location, written = f"s3://{estate.bucket}/reg", estate.store.options().in_clear()
    lance.write_dataset(pa.table({"id": pa.array([1], pa.int64())}), location, storage_options=written, data_storage_version="2.2", enable_stable_row_ids=True)
    for n in (2, 3):
        lance.write_dataset(pa.table({"id": pa.array([n], pa.int64())}), location, mode="overwrite", storage_options=written)
    _ok(estate.catalog.post(f"/v1/table/{NS}$r/register", json={"location": "reg"}))
    _ok(estate.catalog.post(f"/management/v1/table/{NS}$r/policy/set", json={"retain_versions": 1}))
    described = _ok(estate.catalog.post(f"/v1/table/{NS}$r/describe", json={}))
    return estate.model_copy(update={"task": TableTask(uuid=described["properties"]["rask.uuid"], location=described["location"])})


@pytest.mark.parametrize("ref", ["branch", "main"])
def test_a_batch_names_the_incarnation_its_versions_were_listed_on(estate: _Estate, monkeypatch: pytest.MonkeyPatch, ref: str) -> None:
    """ListedOn (ADR 0246 §15): a pass reads its ref's identity, opens the ref and lists the versions past retention, and
    is held as it asks for its batch. The ref is then made again: branch ``b`` (3 to 7, of which a retention of two lists
    3, 4 and 5) deleted and made again under its name from main 3 and written twice, so the new incarnation holds 3, 4
    and 5 again and keeps 4 and 5; or a registered table deregistered and registered again at its location, another uuid
    under no policy of its own, which keeps every version a week. Let go, the batch is refused (409, code 14) and nothing
    of the ref made again is deleted; the next pass reclaims the new branch by its own listing. A batch that took the
    numbers on whichever incarnation stands at its condemn deletes ``b``'s 4, which its retention keeps, or main's 1
    and 2."""
    from maintenance.services import credentials

    table = T
    if ref == "branch":
        estate.passed()
        _ok(estate.catalog.post(f"/management/v1/table/{T}/policy/set", json={"retain_versions": 2}))
        _ok(estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "b", "from_version": 3}))
        estate.written(4, branch="b")
    else:
        estate, table = _registered(estate), f"{NS}$r"
    on = "b" if ref == "branch" else None
    asked = _Pause(monkeypatch, credentials, "condemn", after=False, when=lambda *_args, branch, **_kwargs: branch == on)
    passing = _Running(estate.passed)
    assert asked.reached.wait(WAIT), "the pass never asked for a batch"
    if ref == "branch":
        _ok(estate.catalog.post(f"/v1/table/{T}/branches/delete", json={"name": "b"}))
        _ok(estate.catalog.post(f"/v1/table/{T}/branches/create", json={"name": "b", "from_version": 3}))
        estate.written(2, branch="b")
    else:
        _ok(estate.catalog.post(f"/v1/table/{table}/deregister", json={}))
        _ok(estate.catalog.post(f"/v1/table/{table}/register", json={"location": "reg"}))
    asked.let_go()
    passed = passing.joined()

    if ref == "main":
        assert (estate.versions(), estate.reads(1)) == ([1, 2, 3], [1]), "the pass deleted versions of the table registered again"
    else:
        assert (estate.versions("b"), estate.reads(("b", 4))) == ([3, 4, 5], [100]), "the pass deleted versions of b made again"
    refused = [result.refused or "" for result in passed.results if result.uri.endswith("/tree/b") == (ref == "branch")]
    assert any("(409, code 14)" in reason and "listed on" in reason for reason in refused), refused
    if ref == "main":
        return
    estate.passed()
    assert estate.versions("b") == [4, 5], "the next pass did not reclaim b made again by its own listing"


def test_a_pass_deletes_only_under_a_vend_the_store_refuses_by_its_licences_end(estate: _Estate) -> None:
    """C (ADR 0246 §14): a batch's licence ends at its instant, after which its name may mean another incarnation, so a
    pass deletes a batch's versions only under a vend whose stamped expiry, the clocks' margin added, is no later
    (``condemned_versions.covers``). A vend living longer than the licence is refused: the first batch, asked before the
    vend, is asked again and still not covered, so nothing is deleted and the versions stand condemned. A pass under a
    vend the door caps (``CREDENTIAL_LIFETIME``) then finishes them."""
    estate.passed(vend_lives=condemned_versions.LICENCE * 2)
    assert ({1, 2} <= set(estate.versions()), {(ref, version) for ref, version, _state in _standing_condemned(estate)}) == (True, {(None, 1), (None, 2)})

    estate.passed()
    assert (set(estate.versions()) & {1, 2}, _standing_condemned(estate)) == (set(), []), "a pass under a vend its licence covers did not finish them"


def test_a_maintain_vend_dies_by_the_licence_of_a_batch_asked_after_it(estate: _Estate) -> None:
    """C (ADR 0246 §14): the maintain vend door caps the session at ``CREDENTIAL_LIFETIME`` whatever the vending TTL (here
    an hour), so a batch asked after the vend licenses until an instant its stamped expiry, the clocks' margin added,
    reaches no later than (``covers``). Uncapped, a vend would outlive the licence of every batch a pass asks after it."""
    from catalog.api.dependencies import get_vendor
    from catalog.core.vending import TableRecords, VendedSession

    class _Vendor:
        """A vendor issuing each session for what it is asked, at most the vending TTL, from now."""

        def vend(
            self,
            *,
            table_location: str,
            tier: str,
            session_name: str,
            bases: Any = (),  # noqa: ANN401 — the vendor protocol's
            data_directories: Any = (),  # noqa: ANN401 — the vendor protocol's
            branch: str = "",
            records: TableRecords | None = None,
            at_most_seconds: int | None = None,
        ) -> VendedSession:
            lives = timedelta(seconds=min(3600, at_most_seconds or 3600))
            key_pair = S3KeyPair(access_key_id=estate.store.access_key, secret_access_key=SecretStr(estate.store.secret_key))
            return VendedSession(key_pair=key_pair, endpoint=None, region="us-east-1", expires_at_millis=int((datetime.now(UTC) + lives).timestamp() * 1000))

    estate.catalog.app.dependency_overrides[get_vendor] = lambda: _Vendor()
    vended = _ok(estate.catalog.post(f"/management/v1/table/{T}/credentials?tier=maintain"))
    verdict = condemned_versions.Verdict.model_validate(
        _ok(estate.catalog.post(f"/management/v1/table/{T}/maintenance/condemn", json={"listed_on": estate.task.uuid, "versions": [1]}))
    )
    expires = datetime.fromtimestamp(vended["credentials"]["expires_at_millis"] / 1000, UTC)

    assert verdict.versions == [1] and condemned_versions.covers(verdict, condemned_versions.Credential(dataset=None, expires_at=expires)), (
        f"the vend expires at {expires}, the licence ends at {verdict.licensed_until}"
    )


def test_version_delete_never_leaves_a_tagged_version_condemned(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """VDeleteCondemnsFirst: ``version/delete`` condemns, then reads the tags and pins, and takes back what one names (409,
    naming it), as a batch does. Held after that read, a tag door on the same version pins, then reads the record and
    is refused (409, code 19); either way no version a caller's tag names stays condemned. One that read the tags first
    would condemn, after the tag door read no entry, the version its tag then names: hidden from ``version/list`` and
    refused to every ref door for as long as the tag stands."""
    from catalog.services import maintenance as catalog_maintenance

    vd_read = _Pause(monkeypatch, catalog_maintenance, "refuse_pinned_versions", after=True)
    deleting = _Running(lambda: estate.catalog.post(f"/v1/table/{T}/version/delete", json={"ranges": [{"start_version": 2, "end_version": 3}]}))
    assert vd_read.reached.wait(WAIT), "version/delete never read the tags"
    tagged = estate.catalog.post(f"/v1/table/{T}/tags/create", json={"tag": "kept", "version": 2})
    vd_read.let_go()
    deleted = deleting.joined()

    assert tagged.status_code in (200, 409) and deleted.status_code in (200, 409), (tagged.text, deleted.text)
    assert not (tagged.status_code == 200 and (None, 2) in {(ref, version) for ref, version, _state in estate.condemned()}), (
        f"version/delete answered {deleted.status_code} and left version 2, which tag 'kept' names, condemned"
    )


def test_an_erasures_reclaim_reads_no_maintenance_hold(estate: _Estate) -> None:
    """V7 (ADR 0246 §14): which of an erasure and a retention hold wins is unchanged by this design and open for the owner:
    an erasure's reclaim reads no maintenance hold, as it read none before, so its cleanup of a held table goes through
    (its ``history:main`` surface reclaimed) and the versions past its retention are gone."""
    from catalog.core.config import get_settings
    from catalog.core.namespace import NO_WAREHOUSE
    from catalog.services import reclaims, table_claims
    from catalog.services.erasure import ErasureReport, reclaim_refs

    _ok(estate.catalog.post(f"/management/v1/table/{T}/policy/set", json={"cleanup_enabled": False}))
    settings = get_settings()
    so = settings.storage_options()
    governance = reclaims.Governance(
        registry_root=settings.registry_root,
        storage_options=so,
        claims=table_claims.claim_store(settings),
        location=estate.task.location,
        table_uuid=estate.task.uuid,
        namespace=NO_WAREHOUSE,
    )
    main = lance.dataset(estate.task.location, storage_options=so.in_clear())
    report = ErasureReport(table=T, predicate="id = 1")
    reclaim_refs(report, main, [None], clean={None}, retention=timedelta(0), storage_options=so, condemn=reclaims.erasure_reclaims(governance, settings, main))

    assert ([surface.outcome for surface in report.surfaces], estate.versions()) == (["reclaimed"], [3]), report.surfaces


def _planted_record(estate: _Estate, entries: list[condemned_versions.Entry]) -> None:
    """The table's condemned record, written as the catalog writes it."""
    from catalog.core.config import get_settings
    from lakehouse import records

    settings = get_settings()
    body = condemned_versions.Record(uuid=estate.task.uuid, location=estate.task.location, entries=entries).model_dump(mode="json")
    records.create_json(settings.registry_root, settings.storage_options(), condemned_versions.record_key(estate.task.uuid), body)


def test_a_policy_door_names_at_most_its_bound_and_what_it_could_not_read(estate: _Estate) -> None:
    """V4 (ADR 0246 §14): a namespace's policy door lists the condemned records once and reads those standing, which are
    the tables with a reclaim in flight or stuck: a record is deleted, under the ETag read, with its last entry. Its
    answer names at most 100 versions and counts the rest; a record that does not parse is named, and the policy set
    answers 200 (it landed). Once a reclaim finishes the table's entries, its record is gone."""
    from catalog.core.config import get_settings

    _planted_record(estate, [condemned_versions.Entry(ref_id=condemned_versions.MAIN, version=1000 + n, batch="dead", state="c") for n in range(120)])
    malformed = "_condemned/table-" + "f" * 24 + ".json"
    estate.s3().put_object(Bucket=estate.bucket, Key=malformed, Body=b"{not a record")
    answered = _ok(estate.catalog.post(f"/management/v1/namespace/{NS}/policy/set", json={"retain_versions": 1}))
    assert (len(answered["condemned"]), answered["condemned_beyond"], answered["condemned_unread"]) == (100, 20, [malformed]), answered

    estate.s3().delete_object(Bucket=estate.bucket, Key=malformed)
    estate.passed()
    estate.passed()
    settings = get_settings()
    recorded = condemned_versions.recorded(settings.registry_root, settings.storage_options(), estate.task.uuid)
    assert (estate.versions(), recorded) == ([3], False), "a record whose last entry went still stands"


@pytest.mark.parametrize("payload_bytes", [_SIDECAR])
def test_only_another_batchs_go_entry_licenses_ignoring_a_pin(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """GoOnly, on Lance: a tag door pinned version 1 and read no entry, and is held before it acts; a first batch (asked of
    ``maintenance/condemn`` for version 1, which no dry run names while the door's pin stands) condemned it and is held before its pin read; a second batch (``maintenance/run``) finds the
    door's pin on 1, which only a ``g`` entry of another batch licenses ignoring (that batch read the pins before the door
    pinned, so the door reads its entry and refuses). The first batch's entry is ``c``, so the second keeps 1, and the
    door's tag, landing while the second is held at a manifest delete, names a version that reads whole. Licensing any
    other batch's entry would delete the pin and version 1 under the tag."""
    door_read = _Pause(monkeypatch, condemned_versions, "condemned", after=True)
    answers = _Running(lambda: _ref_door(estate, "tag", 1))
    assert door_read.reached.wait(WAIT), "the door never read the record"
    first = _Pause(monkeypatch, condemned_versions.Batch, "condemn", after=True)
    passing = _Running(lambda: estate.catalog.post(f"/management/v1/table/{T}/maintenance/condemn", json={"listed_on": estate.task.uuid, "versions": [1]}))
    assert first.reached.wait(WAIT), "the first batch never condemned"
    with estate.proxy.holding(_a_manifest_delete) as held:
        second = _Running(lambda: estate.catalog.post(f"/management/v1/table/{T}/maintenance/run", json={"retain_versions": 1}))
        second.held_at(held)
        door_read.let_go()
        answered = answers.joined()
        held.let_go()
        second.joined()
    first.let_go()
    passing.joined()

    assert answered.status_code in (200, 409), answered.text
    assert answered.status_code != 200 or _made(estate, "tag") == [1], f"the tag answered 200 for version 1, which reads {_made(estate, 'tag')}"
