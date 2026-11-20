"""A door that changes state files what it will announce before the change, so a door that stops after its change is
still announced, once, by the finish maintenance's hourly audit asks for ([[LH-576]], ADR 0205).

Each door here is stopped at its first send, as a process ending between its change and its announcement would stop
it. Once the filing is older than a door can act, the finish door sends what the filing names wherever the store shows
the change landed, as the event the door built: a control event under the id the door gave it, a lineage marker at the
time the door fixed, which its run id is derived from. A change the store shows did not land is announced by nobody.

Against the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA deciding on a store of the test's
own ([[LH-235]]), on a real ``dir`` namespace.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any, Unpack

import lance
import pyarrow as pa
import pytest
from announcement_filings import age_filings

from catalog.api.dependencies import get_control_emitter
from catalog.core.config import get_settings
from catalog.core.lineage_emit import EmitFields
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.events.control_events import CatalogControlEvent


ADMIN, MEMBER, RECLAIMER = "alice", "carol", "maintenance"
_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64())}))


class _Stopped(Exception):
    """The process a door ran in ended at the send it raises from."""


class _Lanes:
    """The control lane and the lineage lane, as what each was sent. While ``stopping`` is set, the first send on either
    is recorded and then stops the door, as a process ending right before its announcement went out would."""

    def __init__(self) -> None:
        self.events: list[CatalogControlEvent] = []
        #: ``(operation, table id, event time, table uuid, namespace uuid)`` of each marker sent.
        self.markers: list[tuple[str, str, str | None, str | None, str | None]] = []
        self.stopping = False

    def _stop(self) -> None:
        if self.stopping:
            self.stopping = False
            raise _Stopped

    async def emit(self, event: CatalogControlEvent) -> None:
        self.events.append(event)
        self._stop()

    async def project_for(self, warehouse_id: str) -> str | None:
        del warehouse_id
        return None

    async def emit_create(self, *, table_id: str, namespace: str, author: str | None, version: int, **fields: Unpack[EmitFields]) -> None:
        del table_id, namespace, author, version, fields

    async def emit_write(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int | None,
        operation: str,
        **fields: Unpack[EmitFields],
    ) -> None:
        del namespace, version
        uuids = fields.get("uuids")
        self.markers.append(
            (
                operation,
                table_id,
                fields.get("event_time"),
                uuids.table_uuid if uuids is not None else None,
                uuids.namespace_uuid if uuids is not None else None,
            )
        )
        self._stop()


@pytest.fixture
def estate(governed_catalog: Any) -> Iterator[tuple[Any, _Lanes]]:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's project acme with carol its member, alice's namespace ``lance_catalog$db``, and maintenance holding the estate's
    ``reclaimer`` role, which the finish door asks; both lanes recorded from here on."""
    catalog = governed_catalog
    catalog.store.write((catalog.person(ADMIN), "admin", "estate:rask"), (catalog.person(RECLAIMER), "reclaimer", "estate:rask"))
    made = catalog.client.post("/management/v1/projects", json={"id": "acme"}, headers=catalog.bearer(ADMIN))
    assert made.status_code == 200, made.text
    made = catalog.create_in_the_estate(ADMIN, "db")
    assert made.status_code == 200, made.text
    catalog.store.write((catalog.person(MEMBER), "member", "project:acme"))
    lanes = _Lanes()
    app = catalog.client.app
    kept = app.state.lineage_emitter
    app.dependency_overrides[get_control_emitter] = lambda: lanes
    app.state.lineage_emitter = lanes
    yield catalog, lanes
    app.state.lineage_emitter = kept


def _stopped_after_its_change(catalog: Any, lanes: _Lanes, method: str, path: str, body: dict[str, Any]) -> int | None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """Call the door with its first send stopping it, then age what it filed past the lease; the door's status, ``None``
    for a door that stopped."""
    lanes.stopping = True
    try:
        status: int | None = catalog.client.request(method, path, json=body, headers=catalog.bearer(ADMIN)).status_code
    except _Stopped:
        status = None
    finally:
        lanes.stopping = False
    age_filings()
    return status


def _finish(catalog: Any) -> dict[str, int]:  # noqa: ANN401 — the conftest's GovernedCatalog
    finished = catalog.client.post("/management/v1/announcement-intents/finish", headers=catalog.bearer(RECLAIMER))
    assert finished.status_code == 200, finished.text
    return finished.json()


@pytest.mark.parametrize(
    ("revoked", "answered", "finished"),
    [
        pytest.param(MEMBER, None, {"finished": 1, "sent": 1, "unlanded": 0, "young": 0, "failed": 0, "left": 0}, id="a-revoke-that-landed"),
        pytest.param(ADMIN, 409, {"finished": 1, "sent": 0, "unlanded": 1, "young": 0, "failed": 0, "left": 0}, id="the-last-admin-the-door-refused"),
    ],
)
def test_a_revoke_whose_door_stopped_after_it_is_announced_by_the_finish(
    estate: tuple[Any, _Lanes],
    revoked: str,
    answered: int | None,
    finished: dict[str, int],
) -> None:
    """A revoke that landed tells its person once, under the id its door filed it with; one the door refused, which left
    its filing too, tells nobody."""
    catalog, lanes = estate
    relation = "member" if revoked == MEMBER else "admin"

    status = _stopped_after_its_change(catalog, lanes, "DELETE", "/management/v1/projects/acme/members", {"user": f"dex~{revoked}", "relation": relation})
    attempted = list(lanes.events)

    assert (status, _finish(catalog)) == (answered, finished)
    told = [(event.action, event.event_id, event.extra.get("subject")) for event in lanes.events]
    assert told == [(event.action, event.event_id, event.extra.get("subject")) for event in attempted] * 2
    assert told == ([("grant_revoked", attempted[0].event_id, catalog.person(MEMBER))] * 2 if revoked == MEMBER else [])
    assert _finish(catalog) == {"finished": 0, "sent": 0, "unlanded": 0, "young": 0, "failed": 0, "left": 0}


def _declared(catalog: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    del catalog


def _created(catalog: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    made = catalog.client.post("/v1/table/lance_catalog$db$t/create", content=_ROWS, headers={**catalog.bearer(ADMIN), "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert made.status_code == 200, made.text


def _written_outside(catalog: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    del catalog
    lance.write_dataset(
        pa.table({"id": pa.array([1, 2], pa.int64())}), f"{get_settings().root}/outside/t", data_storage_version="2.2", enable_stable_row_ids=True
    )


def _nested(catalog: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    made = catalog.client.post("/v1/namespace/lance_catalog$db$sub/create", json={}, headers=catalog.bearer(ADMIN))
    assert made.status_code == 200, made.text
    made = catalog.client.post(
        "/v1/table/lance_catalog$db$sub$t/create", content=_ROWS, headers={**catalog.bearer(ADMIN), "Content-Type": ARROW_STREAM_MEDIA_TYPE}
    )
    assert made.status_code == 200, made.text


@pytest.mark.parametrize(
    ("arranged", "path", "body", "operation", "named", "told", "unlanded"),
    [
        pytest.param(_declared, "/v1/table/lance_catalog$db$t/declare", {}, "declare_table", "lance_catalog$db$t", "table_declared", 0, id="declare"),
        pytest.param(
            _written_outside,
            "/v1/table/lance_catalog$db$t/register",
            {"location": "outside/t"},
            "register_table",
            "lance_catalog$db$t",
            "table_registered",
            0,
            id="register",
        ),
        pytest.param(
            _created, "/v1/table/lance_catalog$db$t/rename", {"new_table_name": "u"}, "rename_table", "lance_catalog$db$u", "table_renamed", 0, id="rename"
        ),
        pytest.param(_created, "/v1/table/lance_catalog$db$t/drop", {}, "drop_table", "lance_catalog$db$t", "table_dropped", 0, id="drop"),
        # Stopped at its one table's marker, a cascade has dropped that table and not yet its namespace.
        pytest.param(
            _nested, "/v1/namespace/lance_catalog$db$sub/drop", {"behavior": "Cascade"}, "drop_table", "lance_catalog$db$sub$t", None, 1, id="namespace-cascade"
        ),
    ],
)
def test_a_tables_lifecycle_door_that_stopped_after_its_change_is_announced_by_the_finish(
    estate: tuple[Any, _Lanes],
    arranged: Callable[[Any], None],
    path: str,
    body: dict[str, Any],
    operation: str,
    named: str,
    told: str | None,
    unlanded: int,
) -> None:
    """A table declared, registered, renamed or dropped, on its own or by its namespace's cascade, by a door that stopped
    before announcing it reaches lineage as the run the door would have recorded, and the control plane as the event the
    door would have sent, once; what the door had not done yet is announced by nobody."""
    catalog, lanes = estate
    arranged(catalog)
    lanes.markers.clear()
    lanes.events.clear()

    status = _stopped_after_its_change(catalog, lanes, "POST", path, body)
    attempted = list(lanes.markers)

    sent = 2 - unlanded
    assert (status, _finish(catalog)) == (None, {"finished": 1, "sent": sent, "unlanded": unlanded, "young": 0, "failed": 0, "left": 0})
    assert [marker[:2] for marker in attempted] == [(operation, named)]
    assert lanes.markers == attempted * 2, "the finish's marker is another run than the one the door filed"
    expected = [(told, named, f"table:{attempted[0][3]}")] if told is not None else []
    assert [(event.action, event.object_name, event.object_id) for event in lanes.events] == expected


@pytest.fixture
def kept_by_the_trash(monkeypatch: pytest.MonkeyPatch) -> None:
    """An estate whose drops the trash keeps for a week, set before the app reads its settings."""
    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "7")
    get_settings.cache_clear()


class _Ended(BaseException):
    """The process a door ran in ended here: no handler of the door's runs after it, so it lifts no fence and settles nothing."""


def _ended(*_args: object, **_kwargs: object) -> None:
    raise _Ended


def _lapse_the_fence_on(location: str) -> None:
    """Stamp the fence on ``location``'s claim twice the lease earlier, as a fence whose request ended that long ago."""
    from catalog.services import table_claims
    from lakehouse.records import location_claims
    from lakehouse.storage import conditional_records

    settings = get_settings()
    found = location_claims.read(table_claims.claim_store(settings), location)
    assert found is not None and found.claim.fence is not None, "the drop fenced the claim before it ended"
    fence = found.claim.fence.model_copy(update={"at": found.claim.fence.at - 2 * location_claims.LEASE})
    lapsed = found.claim.model_copy(update={"fence": fence}).model_dump(mode="json")
    conditional_records.replace_json(settings.registry_root, settings.storage_options(), location_claims.claim_key(location), lapsed, etag=found.etag)


def _filed_ids(filing: Any) -> tuple[list[str], list[str]]:  # noqa: ANN401 — announcement_intents.Standing
    """The control event ids and the lineage markers' times (their run ids derive from them) a door filed."""
    from catalog.services.announcement_intents import ControlMessage, LineageMarker

    messages = [announcement.message for announcement in filing.intent.announcements]
    return [m.event.event_id for m in messages if isinstance(m, ControlMessage)], [m.event_time for m in messages if isinstance(m, LineageMarker)]


@pytest.mark.parametrize("key", [None, "0b6f3f0e-5c7a-4a43-9d6e-2f1c8a9b7e13"], ids=["unkeyed", "keyed"])
@pytest.mark.parametrize(
    "ended",
    [
        "its-detach-failed-then-it-was-renamed",
        "it-ended-before-its-detach-then-it-was-renamed",
        "it-ended-after-its-detach-then-it-was-finished",
        "it-ended-after-its-detach-then-it-was-undropped",
    ],
)
def test_a_recoverable_drop_that_stopped_is_announced_only_where_it_detached_the_table(
    kept_by_the_trash: None, estate: tuple[Any, _Lanes], monkeypatch: pytest.MonkeyPatch, ended: str, key: str | None
) -> None:
    """A recoverable drop files its trash record, detaches the name, then stamps the record as detached. One whose detach
    failed, or whose process ended before it, leaves the table live, renamed since: no drop is announced, and the record
    the drop filed is taken back, so the live table is not kept in the trash (where maintenance passes it by). One whose
    process ended after the detach and before the stamp dropped the table: it is announced once, as filed, by the finish,
    before which its grants are not released, or by the undrop that restores it, which waits for the drop's fence."""
    from catalog.services import announcement_intents, native
    from lakehouse.records import trash

    catalog, lanes = estate
    _created(catalog)
    described = catalog.client.post("/v1/table/lance_catalog$db$t/describe", json={}, headers=catalog.bearer(ADMIN)).json()
    uuid, location = described["properties"]["rask.uuid"], described["location"]
    lanes.markers.clear()
    lanes.events.clear()
    detach = native.call
    stopped = RuntimeError("the backend refused the detach") if ended.startswith("its-detach-failed") else _Ended()

    def detach_then_stop(ns: Any, op: str, request: object) -> object:  # noqa: ANN401 — a LanceNamespace
        if op == "deregister_table":
            raise stopped
        return detach(ns, op, request)

    with monkeypatch.context() as during:
        if ended.endswith("then-it-was-renamed"):
            during.setattr(native, "call", detach_then_stop)
        else:
            during.setattr(trash, "stamp_detached", _ended)
        headers = {**catalog.bearer(ADMIN), **({"Idempotency-Key": key} if key else {})}
        with contextlib.suppress(_Ended, RuntimeError):  # the test client raises what the door raised, or is answered 500
            catalog.client.post("/v1/table/lance_catalog$db$t/drop", headers=headers)
    settings = get_settings()
    root, so = settings.registry_root, settings.storage_options()
    (filing,) = announcement_intents.standing(root, so).filings
    events, times = _filed_ids(filing)
    if ended.endswith("then-it-was-undropped"):
        age_filings()
        undrop = "/management/v1/table/lance_catalog$db$t/undrop"
        # The drop's filing has aged and its fence stands: the undrop's judgement of the drop cannot tell yet, so the undrop
        # acts on nothing rather than restoring a table whose drop nobody was told of.
        refused = catalog.client.post(undrop, json={}, headers=catalog.bearer(ADMIN))
        assert (refused.status_code, refused.json().get("code")) == (409, 14), f"an undrop restored a table whose drop was not yet told: {refused.text}"
        _lapse_the_fence_on(location)
        undropped = catalog.client.post(undrop, json={}, headers=catalog.bearer(ADMIN))
        assert undropped.status_code == 200, undropped.text
        told = [(event.action, event.event_id) for event in lanes.events if event.action in {"table_dropped", "table_undropped"}]
        assert told[:1] == [("table_dropped", events[0])] and [action for action, _ in told] == ["table_dropped", "table_undropped"], told
        assert [(marker[0], marker[2]) for marker in lanes.markers][:1] == [("drop_table", times[0])], lanes.markers
        return
    if ended.startswith("it-ended"):
        # A process that ended lifted no fence: the table is the drop's until the fence lapses, and so is its filing.
        age_filings()
        _lapse_the_fence_on(location)
    if ended.endswith("then-it-was-renamed"):
        renamed = catalog.client.post("/v1/table/lance_catalog$db$t/rename", json={"new_table_name": "m"}, headers=catalog.bearer(ADMIN))
        assert renamed.status_code == 200, renamed.text
    age_filings()
    release = f"/management/v1/trash/table/{uuid}/release"
    if ended.endswith("then-it-was-finished"):
        kept = trash.get(root, so, uuid)
        assert kept is not None
        trash.put(root, so, kept.model_copy(update={"expires_at": "2000-01-01T00:00:00+00:00"}))
        refused = catalog.client.post(release, json={}, headers=catalog.bearer(RECLAIMER))
        assert (refused.status_code, refused.json().get("code")) == (409, 19), f"a drop's grants were released before the drop was announced: {refused.text}"
        assert "announcements on the object are not sent yet" in refused.json().get("detail", ""), refused.text

    finished = _finish(catalog)

    dropped = [event.event_id for event in lanes.events if event.action == "table_dropped"]
    markers = [(marker[1], marker[2]) for marker in lanes.markers if marker[0] == "drop_table"]
    if ended.endswith("then-it-was-renamed"):
        assert (finished["sent"], dropped, markers) == (0, [], []), "a drop that never detached its table is announced"
        moved = catalog.client.post("/v1/table/lance_catalog$db$m/describe", json={}, headers=catalog.bearer(ADMIN))
        assert moved.status_code == 200, moved.text
        assert not trash.keeps(root, so, uuid), "the live table is still kept in the trash"
    else:
        assert (finished["sent"], dropped, markers) == (2, events, [("lance_catalog$db$t", times[0])])
        kept = trash.get(root, so, uuid)
        assert kept is not None and kept.detached, "the finish leaves the dropped table's record unstamped"
        released = catalog.client.post(release, json={}, headers=catalog.bearer(RECLAIMER))
        assert released.status_code == 200, released.text
    assert _finish(catalog)["finished"] == 0


@pytest.mark.parametrize(
    "lost", ["its-record-before-its-stamp", "its-record-while-its-detach-was-in-flight", "its-fence-to-another-drop", "its-fence-before-its-detach-returned"]
)
def test_a_recoverable_drop_that_stalled_past_its_fence_acts_only_while_it_holds_it(
    kept_by_the_trash: None, estate: tuple[Any, _Lanes], monkeypatch: pytest.MonkeyPatch, lost: str
) -> None:
    """A drop acts on its trash record or the table's registration only while its fence stands. One whose detach returned
    while it held the fence files its record again where the finish withdrew it, so the dropped table stays undroppable.
    One that lost its fence to another drop does not detach, and touches no record: the other drop's stands. One whose
    detach returned after the fence lapsed, the finish having told the drop and an undrop restored the table meanwhile,
    files nothing: the live table is kept by no trash record, and the drop and the undrop are each told once."""
    from catalog.services import native, table_claims
    from lakehouse.records import location_claims, trash
    from lakehouse.storage import conditional_records

    catalog, lanes = estate
    _created(catalog)
    described = catalog.client.post("/v1/table/lance_catalog$db$t/describe", json={}, headers=catalog.bearer(ADMIN)).json()
    uuid, location = described["properties"]["rask.uuid"], described["location"]
    lanes.events.clear()
    settings = get_settings()
    root, so = settings.registry_root, settings.storage_options()
    detach, filed, deleted = native.call, trash.put, trash.delete_record
    other: list[trash.TrashRecord] = []
    theirs: list[trash.TrashRecord] = []

    def withdrawn_then_detached(ns: Any, op: str, request: object) -> object:  # noqa: ANN401 — a LanceNamespace
        if op == "deregister_table":
            trash.clear(root, so, uuid)
        return detach(ns, op, request)

    def withdrawn_by_the_finish_then_detached(ns: Any, op: str, request: object) -> object:  # noqa: ANN401 — a LanceNamespace
        if op == "deregister_table":
            # The detach is in flight past the lease: the finish judges the drop while the table still resolves.
            _lapse_the_fence_on(location)
            age_filings()
            _finish(catalog)
            assert not trash.keeps(root, so, uuid), "the finish did not withdraw the record of a table that still resolved"
        return detach(ns, op, request)

    def detached_then_returned_late(ns: Any, op: str, request: object) -> object:  # noqa: ANN401 — a LanceNamespace
        answer = detach(ns, op, request)
        if op == "deregister_table":
            _lapse_the_fence_on(location)
            age_filings()
            _finish(catalog)
            restored = catalog.client.post("/management/v1/table/lance_catalog$db$t/undrop", json={}, headers=catalog.bearer(ADMIN))
            assert restored.status_code == 200, restored.text
        return answer

    def filed_then_fenced_by_another(control_root: str, options: Any, record: trash.TrashRecord) -> None:  # noqa: ANN401 — StorageOptions
        filed(control_root, options, record)
        found = location_claims.read(table_claims.claim_store(settings), record.location)
        assert found is not None and found.claim.fence is not None
        taken = found.claim.model_copy(update={"fence": found.claim.fence.model_copy(update={"token": "f" * 32})}).model_dump(mode="json")
        conditional_records.replace_json(root, so, location_claims.claim_key(record.location), taken, etag=found.etag)
        other.append(record.model_copy(update={"drop": "f" * 32}))
        theirs.append(other[0])

    def deleted_after_another_drop_filed(control_root: str, options: Any, key: str) -> bool:  # noqa: ANN401 — StorageOptions
        # The other drop's record lands between a read of the record and its delete.
        if other and key == trash.key(uuid):
            filed(control_root, options, other.pop())
        return deleted(control_root, options, key)

    with monkeypatch.context() as during:
        if lost == "its-record-before-its-stamp":
            during.setattr(native, "call", withdrawn_then_detached)
        elif lost == "its-record-while-its-detach-was-in-flight":
            during.setattr(native, "call", withdrawn_by_the_finish_then_detached)
        elif lost == "its-fence-to-another-drop":
            during.setattr(trash, "put", filed_then_fenced_by_another)
            during.setattr(trash, "delete_record", deleted_after_another_drop_filed)
        else:
            during.setattr(native, "call", detached_then_returned_late)
        dropped = catalog.client.post("/v1/table/lance_catalog$db$t/drop", headers=catalog.bearer(ADMIN))

    told = [event.action for event in lanes.events if event.action in {"table_dropped", "table_undropped"}]
    if lost in {"its-record-before-its-stamp", "its-record-while-its-detach-was-in-flight"}:
        assert dropped.status_code == 200, dropped.text
        kept = trash.get(root, so, uuid)
        assert kept is not None and kept.detached, "the dropped table was left with no trash record"
        assert told == ["table_dropped"], told
        undropped = catalog.client.post("/management/v1/table/lance_catalog$db$t/undrop", json={}, headers=catalog.bearer(ADMIN))
        assert undropped.status_code == 200, undropped.text
    elif lost == "its-fence-to-another-drop":
        assert (dropped.status_code, dropped.json().get("code")) == (409, 14), dropped.text
        if other:  # this drop opened no window between a read and a delete: the other drop files its record now
            filed(root, so, other.pop())
        age_filings()
        _finish(catalog)
        stands = conditional_records.read_json(root, so, trash.key(uuid))
        assert stands is not None and stands[0] == theirs[0].stored(), "a drop that lost its fence removed the record another drop filed"
        assert told == []
    else:
        assert (dropped.status_code, dropped.json().get("code")) == (409, 14), dropped.text
        assert not trash.keeps(root, so, uuid), "a drop whose fence lapsed filed a record on the table an undrop restored"
        assert told == ["table_dropped", "table_undropped"], told
    described = catalog.client.post("/v1/table/lance_catalog$db$t/describe", json={}, headers=catalog.bearer(ADMIN))
    assert described.status_code == 200, described.text


@pytest.mark.parametrize("limit", [200, 1], ids=["one-call", "a-call-per-filing"])
def test_a_finish_pass_finishes_every_filing_behind_one_it_cannot_judge(
    kept_by_the_trash: None, estate: tuple[Any, _Lanes], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, limit: int
) -> None:
    """A filing whose judgement raises (here a drop whose trash record is no record) is left standing, said once, and
    counted `failed`; the filing behind it is finished in the same pass, in the same call, or in the next call of the pass,
    which resumes past what the call before looked at."""
    import logging

    from catalog.api.v1.endpoints import announcement_intents as finish_door
    from catalog.services import announcement_intents
    from lakehouse.records import trash
    from lakehouse.storage.objectfs import fs_and_base

    catalog, lanes = estate
    settings = get_settings()
    root, so = settings.registry_root, settings.storage_options()
    uuids, locations = [], []
    for name in ("t", "u"):
        made = catalog.client.post(
            f"/v1/table/lance_catalog$db${name}/create", content=_ROWS, headers={**catalog.bearer(ADMIN), "Content-Type": ARROW_STREAM_MEDIA_TYPE}
        )
        assert made.status_code == 200, made.text
        described = catalog.client.post(f"/v1/table/lance_catalog$db${name}/describe", json={}, headers=catalog.bearer(ADMIN)).json()
        uuids.append(described["properties"]["rask.uuid"])
        locations.append(described["location"])
        with monkeypatch.context() as during:
            during.setattr(trash, "stamp_detached", _ended)
            with contextlib.suppress(_Ended):
                catalog.client.post(f"/v1/table/lance_catalog$db${name}/drop", headers=catalog.bearer(ADMIN))
        _lapse_the_fence_on(described["location"])
    newest = announcement_intents.standing(root, so).filings[-1]
    events, _times = _filed_ids(newest)
    fs, base = fs_and_base(root, so)
    with fs.open_output_stream(f"{base}/{trash.key(uuids[0])}") as planted:
        planted.write(b"not a trash record")
    age_filings()
    lanes.events.clear()
    monkeypatch.setattr(finish_door, "FINISH_LIMIT", limit)
    caplog.set_level(logging.ERROR)

    totals = {"finished": 0, "sent": 0, "failed": 0}
    cursor: str | None = None
    for _ in range(4):
        answer = catalog.client.post(
            "/management/v1/announcement-intents/finish", params={"after": cursor} if cursor else {}, headers=catalog.bearer(RECLAIMER)
        )
        assert answer.status_code == 200, answer.text
        totals = {name: count + answer.json()[name] for name, count in totals.items()}
        if (cursor := answer.json().get("next")) is None:
            break

    assert totals == {"finished": 1, "sent": 2, "failed": 1}, totals
    assert [event.event_id for event in lanes.events if event.action == "table_dropped"] == events
    kept = trash.get(root, so, uuids[1])
    assert kept is not None and kept.detached
    assert [record.getMessage() for record in caplog.records].count("announcement_intent_unjudged") == 1


def test_a_namespace_undrop_that_stopped_after_restoring_a_table_is_announced_by_the_finish(kept_by_the_trash: None, estate: tuple[Any, _Lanes]) -> None:
    """A namespace's undrop restores each table its cascade trashed, and lineage has to hear of each: a restored table
    nobody announced is a live table its feed never names again. The door files the markers before it rebuilds anything,
    so one that stops at a table's marker leaves the finish to send it, and to tell the control plane of the undrop."""
    catalog, lanes = estate
    _nested(catalog)
    dropped = catalog.client.post("/v1/namespace/lance_catalog$db$sub/drop", json={"behavior": "Cascade"}, headers=catalog.bearer(ADMIN))
    assert dropped.status_code == 200, dropped.text
    assert _finish(catalog)["left"] == 0
    lanes.markers.clear()
    lanes.events.clear()

    status = _stopped_after_its_change(catalog, lanes, "POST", "/management/v1/namespace/lance_catalog$db$sub/undrop", {})
    attempted = list(lanes.markers)

    assert (status, _finish(catalog)) == (None, {"finished": 1, "sent": 2, "unlanded": 0, "young": 0, "failed": 0, "left": 0})
    assert [marker[:2] for marker in attempted] == [("register_table", "lance_catalog$db$sub$t")]
    assert None not in attempted[0][3:], "the restored table is announced without its own or its namespace's uuid"
    assert lanes.markers == attempted * 2
    (told,) = lanes.events
    assert (told.action, told.object_name, told.object_id) == ("namespace_undropped", "lance_catalog$db$sub", f"namespace:{told.object_uuid}")
