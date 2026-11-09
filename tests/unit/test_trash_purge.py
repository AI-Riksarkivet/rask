"""The expired-trash purge (#79) — reclamation, against REAL stores.

This is the first thing in ``services/maintenance`` that deletes bytes it did not write, so the harness
is deliberately the same shape as ``test_drop_protection.py`` / ``test_reconcile_report.py``: a real
``file://`` control root written with the real ``lakehouse.trash`` primitives, and a real
Lance estate built through ``lance_namespace.connect("dir", …)`` — the impl the chart runs. Nothing about
the object store or the trash registry is mocked, because every interesting refusal in this module is a
statement about what is REALLY on disk, and a double would happily agree with a wrong one.

The recoverable drop is reproduced faithfully rather than simulated: a table is created through the
backend and then ``deregister_table``-d, which is exactly what the catalog does when a grace period is
configured — the manifest row goes away, the bytes stay. A trash record over those bytes is therefore the
real thing, not a fixture shaped like one.

The catalog's trash doors are the one seam that is faked ([[XC-084]]), by ``trash_doors``, which acts with the same
``lakehouse.trash`` calls the doors make: the purge deletes a table's bytes under the session the catalog vends and then
asks the catalog to end the record, which revokes the grants it still holds. What the catalog judges before it acts is
its own tests' claim; these hold what the purge does around it, the load-bearing ordering rule included: the record is
ended only once its bytes are gone.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pyarrow as pa
import pytest
import respx
from lance_namespace import (
    CreateNamespaceRequest,
    CreateTableRequest,
    DeregisterTableRequest,
    DescribeTableRequest,
    DropNamespaceRequest,
    RegisterTableRequest,
    connect,
)
from pydantic import SecretStr

from lakehouse import location_claims, trash
from lakehouse.control_events import CatalogControlEvent
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.services import purge as mod
from maintenance.services.reconcile import CATEGORIES, CategorySkipped, CategoryUnavailable, IncompleteScan, ReconcileReport
from tests.integration.maintenance_vend_doors import CATALOG_URL, end_door, trash_doors


_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_ORDERS_UUID = "0b6f7c1e-5a2d-4c8e-9f31-6d2a4e7b9c10"
_TEAM_UUID = "7e3a9d42-1b6c-4f05-8a27-c94d0e5b3f68"
_T_UUID = "c2d81f5a-93e4-4b7d-a610-5f8e2b4c7d93"
_GONE_UUID = "4a9c6e13-d72b-4f80-b5e1-28c7a3f0d6e4"
_ARCHIVE_UUID = "e815b3d7-6f2a-4c94-9d08-b1a7c5e2f3a6"


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


def _settings(tmp_path: Path, **over: Any) -> MaintenanceSettings:
    """Maintenance settings pointed at a local control root, with the purge ARMED unless told otherwise."""
    base: dict[str, Any] = {
        "s3_bucket": "lance-catalog",
        "s3_secret_access_key": SecretStr("unit"),
        "control_root": f"file://{tmp_path / 'control'}",
        "trash_purge_enabled": True,
        "catalog_url": CATALOG_URL,
    }
    return MaintenanceSettings(**{**base, **over})


@pytest.fixture(autouse=True)
def doors(tmp_path: Path) -> Iterator[respx.MockRouter]:
    """The catalog's trash doors over this test's control root, vending a session for every record and ending a record
    as the catalog ends it ([[XC-084]]): which records it acts on is the catalog's own tests' claim."""
    with trash_doors(f"file://{tmp_path / 'control'}", StorageOptions()) as router:
        yield router


def _clean_report(skipped: Iterable[str] = ("orphan_files",)) -> ReconcileReport:
    """A drift report that certifies the estate: every category checked and empty, bar deliberate skips."""
    skips = list(skipped)
    return ReconcileReport(
        checked_at=_NOW.isoformat(),
        counts={category: 0 for category in CATEGORIES if category not in skips},
        total=0,
        skipped=[CategorySkipped(category=category, reason="deliberately off in this configuration") for category in skips],
    )


class _Estate:
    """A real ``dir``-backend Lance estate plus a real trash registry on a local control root."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.data = tmp_path / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.control_root = f"file://{tmp_path / 'control'}"
        self.data_root = f"file://{self.data}"
        self.ns = connect("dir", {"root": str(self.data)})

    def create_table(self, *segments: str) -> str:
        """Create a REAL table through the backend and return its recorded location."""
        if len(segments) > 1:
            with_parent = list(segments[:-1])
            for depth in range(1, len(with_parent) + 1):
                # already-exists is fine; the estate only needs the parent to be there
                with suppress(Exception):
                    self.ns.create_namespace(CreateNamespaceRequest(id=with_parent[:depth]))
        sink = io.BytesIO()
        arrow = pa.table({"a": pa.array([1, 2, 3])})
        with pa.ipc.new_stream(sink, arrow.schema) as writer:
            writer.write_table(arrow)
        self.ns.create_table(CreateTableRequest(id=list(segments)), sink.getvalue())
        return str(self.ns.describe_table(DescribeTableRequest(id=list(segments))).location)

    def drop_recoverably(
        self,
        *segments: str,
        grace_days: int = 7,
        dropped_at: datetime | None = None,
        uuid: str | None = None,
        namespace_uuid: str | None = None,
        released: bool = True,
    ) -> tuple[str, str]:
        """The catalog's recoverable drop, for real: deregister (bytes stay) + file a trash record.

        Returns ``(canonical_id, location)``. ``dropped_at`` in the past is how a record becomes EXPIRED. ``uuid`` is the
        table's, which the record is filed under and its location's claim keeps, as the catalog's drop keeps both (ADR 0173
        §6, §8); a fresh one when none is named, and an empty one files a record that keeps no uuid to read.
        """
        location = self.create_table(*segments)
        self.ns.deregister_table(DeregisterTableRequest(id=list(segments)))
        canonical = "$".join(segments)
        minted = str(uuid4()) if uuid is None else uuid
        claims = location_claims.ClaimStore(control_root=self.control_root)
        location_claims.take(claims, location, canonical, list(segments), is_gone=lambda _claim: False, uuid=minted or None)
        self.put_record(
            canonical, uuid=minted, location=location, grace_days=grace_days, dropped_at=dropped_at, namespace_uuid=namespace_uuid, released=released
        )
        return canonical, location

    def reregister(self, *segments: str, location: str) -> None:
        """The undrop's re-register, for real: the dropped table's own bytes back under its name, at its location."""
        self.ns.register_table(RegisterTableRequest(id=list(segments), location=location.rstrip("/").rsplit("/", 1)[-1]))

    def put_record(
        self,
        canonical: str,
        *,
        location: str,
        uuid: str | None = None,
        grace_days: int = 7,
        dropped_at: datetime | None = None,
        kind: trash.TrashKind = "table",
        properties: dict[str, str] | None = None,
        namespace_uuid: str | None = None,
        released: bool = True,
    ) -> dict[str, Any]:
        """A trash record, stamped released as the catalog's release door leaves one, unless ``released`` is false."""
        record = trash.make_record(
            canonical,
            uuid=str(uuid4()) if uuid is None else uuid,
            location=location,
            dropped_by="user:alice",
            grace_days=grace_days,
            now=dropped_at or (_NOW - timedelta(days=30)),
            kind=kind,
            properties=properties,
            namespace_uuid=namespace_uuid,
        )
        if released:
            record[trash.RELEASED_AT] = (_NOW - timedelta(days=1)).isoformat()
        trash.put(self.control_root, StorageOptions(), record)
        return record

    def record(self, canonical: str, *, kind: trash.TrashKind = "table") -> dict[str, Any] | None:
        """The one trash record dropped under ``canonical``, or ``None``: each test drops a name once."""
        held = trash.named(self.control_root, StorageOptions(), canonical, kind=kind)
        assert len(held) <= 1, held
        return held[0] if held else None


class _RecordingControl:
    """A control emitter that keeps every event instead of publishing it."""

    def __init__(self) -> None:
        self.events: list[CatalogControlEvent] = []

    async def emit(self, event: CatalogControlEvent) -> None:
        self.events.append(event)


def _fingerprint(root: Path) -> dict[str, str]:
    """Every file under ``root`` by relative path → a hash of its bytes."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def _run(
    estate: _Estate,
    *,
    settings: MaintenanceSettings | None = None,
    report: ReconcileReport | None = None,
    control: _RecordingControl | None = None,
    data_roots: set[str] | None = None,
) -> mod.TrashPurgeReport:
    return asyncio.run(
        mod.purge_expired_trash(
            settings or _settings(estate.tmp_path),
            report=report or _clean_report(),
            control=cast(Any, control) if control is not None else None,
            control_root=estate.control_root,
            data_roots=data_roots if data_roots is not None else {estate.data_root},
        )
    )


# --------------------------------------------------------------------------- #
# the default: report-only, still
# --------------------------------------------------------------------------- #


def test_purge_is_off_by_default(tmp_path: Path) -> None:
    """The shipped configuration reclaims NOTHING.

    Reclamation is opt-in for the same reason the grace period is: turning it on changes what a drop
    means for every caller (past the deadline, unrecoverable). The estate must come out byte-identical
    and the record must survive, so "off" is proven behaviourally rather than by reading the flag back.
    """
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders")
    before = _fingerprint(tmp_path)

    out = _run(estate, settings=_settings(tmp_path, trash_purge_enabled=False))

    assert out.enabled is False
    assert out.ran is False
    assert "MAINTENANCE_TRASH_PURGE_ENABLED is off" in (out.reason or "")
    assert out.purged == [] and out.refused == []
    assert _fingerprint(tmp_path) == before, "the disabled purge touched the estate"
    assert Path(location.removeprefix("file://")).is_dir()
    assert estate.record(canonical) is not None


# --------------------------------------------------------------------------- #
# the gate: the drift report is what earns the delete permission
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda r: (r.counts.__setitem__("ghost_projects", 1), setattr(r, "total", 1)), "NOT clean"),
        (lambda r: r.unavailable.append(CategoryUnavailable(category="ghost_projects", reason="openfga down")), "could not check"),
        (lambda r: r.incomplete.append(IncompleteScan(source="fga:tuples", reason="page ceiling")), "INCOMPLETE"),
    ],
    ids=["drift", "unavailable", "incomplete"],
)
def test_a_drifting_report_blocks_the_purge(tmp_path: Path, mutate: Callable[[ReconcileReport], object], needle: str) -> None:
    """Three ways a report fails to certify the estate, and all three stop reclamation.

    Findings block for the obvious reason. UNAVAILABLE and INCOMPLETE block for the less obvious one:
    those are questions the report tried to answer and could not, and a reclaimer acting on a scan that
    half-failed is precisely the failure the report-first rule exists to prevent. A permanently-broken
    FGA connection must never read as "no drift, go ahead and delete".
    """
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders")
    report = _clean_report()
    mutate(report)
    before = _fingerprint(tmp_path)

    out = _run(estate, report=report)

    assert out.ran is False
    assert needle in (out.reason or ""), out.reason
    assert _fingerprint(tmp_path) == before
    assert Path(location.removeprefix("file://")).is_dir()
    assert estate.record(canonical) is not None


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #


def test_an_unexpired_record_survives(tmp_path: Path) -> None:
    """Inside the grace window nothing happens — the deadline is the whole contract with the dropper."""
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders", grace_days=7, dropped_at=datetime.now(UTC))

    out = _run(estate)

    assert out.ran is True
    assert out.due == 0
    assert out.purged == [] and out.refused == []
    assert Path(location.removeprefix("file://")).is_dir()
    assert estate.record(canonical) is not None


def test_a_namespace_record_is_ended_and_deletes_nothing(tmp_path: Path, doors: respx.MockRouter) -> None:
    """#96: a recoverable CASCADE trashes namespaces too, and a namespace owns no bytes.

    Its record carries ``location=""`` — a manifest ROW is all there ever was — so the purge asks no session, touches
    the data root not at all, and has the catalog end the record under its own kind: a record ended under the wrong kind
    hashes the wrong key, silently no-ops, and re-purges every tick forever.
    """
    estate = _Estate(tmp_path)
    estate.create_table("team", "orders")  # a live sibling, so "nothing deleted" is a real claim
    estate.put_record("archive", uuid=_ARCHIVE_UUID, location="", kind="namespace", properties={"rask.uuid": _ARCHIVE_UUID})
    before = _fingerprint(estate.data)

    out = _run(estate)

    assert [(p.kind, p.id, p.location, p.bytes_deleted) for p in out.purged] == [("namespace", "archive", "", 0)]
    assert _fingerprint(estate.data) == before, "a namespace purge touched the data root"
    assert estate.record("archive", kind="namespace") is None
    assert (doors.routes["credentials"].call_count, doors.routes["end"].call_count) == (0, 1)


# --------------------------------------------------------------------------- #
# the refusal ladder
# --------------------------------------------------------------------------- #


def test_an_unreadable_manifest_refuses_every_record(tmp_path: Path) -> None:
    """A manifest we cannot READ is not a manifest that says "nothing is live".

    Collapsing those two is how a broken index authorises deleting the whole estate. The probe that lets
    a FRESH root answer ``set()`` must not also swallow a corrupt one, so this points the purge at a root
    whose ``__manifest`` has a ``_versions`` directory full of garbage: present, unreadable, refuse all.
    """
    estate = _Estate(tmp_path)
    broken = tmp_path / "broken"
    (broken / "__manifest" / "_versions").mkdir(parents=True)
    (broken / "__manifest" / "_versions" / "1.manifest").write_bytes(b"not a lance manifest")
    orphan = broken / "team$orders"
    orphan.mkdir()
    (orphan / "data.lance").write_bytes(b"payload")
    estate.put_record("team$orders", location=f"file://{orphan}")

    out = _run(estate, data_roots={f"file://{broken}"})

    assert out.ran is True
    assert out.purged == []
    assert "manifest could not be read" in out.refused[0].reason
    assert (orphan / "data.lance").exists()
    assert estate.record("team$orders") is not None


def test_a_location_outside_the_maintained_estate_is_refused(tmp_path: Path) -> None:
    """A record's ``location`` is DATA, not a promise. Nothing constrains what the catalog wrote there.

    A location under a bucket this service does not maintain (another deployment's, a typo, a tampered
    record) is refused rather than deleted — the maintained estate is the boundary of what a reclaimer
    may touch at all.
    """
    estate = _Estate(tmp_path)
    elsewhere = tmp_path / "not-ours" / "team$orders"
    elsewhere.mkdir(parents=True)
    (elsewhere / "data.lance").write_bytes(b"payload")
    estate.put_record("team$orders", location=f"file://{elsewhere}")

    out = _run(estate)

    assert out.purged == []
    assert "outside the maintained estate" in out.refused[0].reason
    assert (elsewhere / "data.lance").exists()
    assert estate.record("team$orders") is not None


def test_a_store_root_location_is_refused(tmp_path: Path) -> None:
    """``location = "s3://bkt"`` would ``delete_dir`` a whole bucket. One malformed record, one estate."""
    estate = _Estate(tmp_path)
    estate.create_table("team", "orders")
    before = _fingerprint(estate.data)
    estate.put_record("team$orders_bad", location=estate.data_root)
    estate.put_record("team$orders_bad_slash", location=f"{estate.data_root}/")

    out = _run(estate)

    assert out.purged == []
    assert {r.id for r in out.refused} == {"team$orders_bad", "team$orders_bad_slash"}
    assert all("IS a store root" in r.reason for r in out.refused)
    assert _fingerprint(estate.data) == before


def test_a_control_prefix_location_is_refused(tmp_path: Path) -> None:
    """The estate's OWN state lives under those prefixes — the registries, the trash, the manifest.

    Checked on EVERY path segment, not just the last: ``…/_trash/x.json`` names a file inside the trash
    registry, and a final-segment-only rule would cheerfully delete it.

    Judged at the place the purge deletes, the location as it decodes ([[LH-319]]): ``%5Ftrash`` opens ``_trash``,
    and a spelling that names no single place once decoded (``x/%2e%2e``) is refused before any path rule.
    """
    estate = _Estate(tmp_path)
    estate.create_table("team", "orders")
    before = _fingerprint(estate.data)
    estate.put_record("evil_manifest", location=f"{estate.data_root}/__manifest")
    estate.put_record("evil_nested", location=f"{estate.data_root}/_trash/some-record.json")
    # [[LH-279]] The catalog's base records: purging one would un-sanction a recorded clone source.
    estate.put_record("evil_bases", location=f"{estate.data_root}/_bases")
    estate.put_record("evil_encoded", location=f"{estate.data_root}/%5Ftrash/some-record.json")
    estate.put_record("evil_dotdot", location=f"{estate.data_root}/x/%2e%2e/_trash")

    out = _run(estate)

    assert out.purged == []
    reasons = {r.id: r.reason for r in out.refused}
    assert sorted(reasons) == ["evil_bases", "evil_dotdot", "evil_encoded", "evil_manifest", "evil_nested"]
    assert all("control prefix" in reasons[record] for record in ("evil_bases", "evil_encoded", "evil_manifest", "evil_nested")), reasons
    assert "names no single place" in reasons["evil_dotdot"], reasons["evil_dotdot"]
    assert _fingerprint(estate.data) == before


def test_a_deactivated_warehouses_bucket_is_outside_the_maintained_estate(tmp_path: Path) -> None:
    """Deactivate is offboarding step one: the resolver already 403s every operation on that tenant.

    Reclaiming its dropped tables would be the one process still destroying data the estate has said
    nobody may touch — and unlike the orphan REPORT (which deliberately still scans a quarantined
    warehouse, because naming findings changes nothing on disk), this one deletes.
    """
    from catalog.services import warehouses as wh_svc

    control_root = f"file://{tmp_path / 'control'}"
    wh_svc.put_warehouse(
        control_root, StorageOptions(), {"id": "wh-live", "bucket": "bkt-live", "root_uri": "s3://bkt-live", "project": "acme", "created_at": "t"}
    )
    wh_svc.put_warehouse(
        control_root,
        StorageOptions(),
        {"id": "wh-gone", "bucket": "bkt-quarantined", "root_uri": "s3://bkt-quarantined", "project": "acme", "created_at": "t", "status": "deactivated"},
    )
    settings = _settings(tmp_path)

    roots = mod.maintained_roots(settings, StorageOptions())

    assert "s3://bkt-live" in roots
    assert "s3://bkt-quarantined" not in roots, "a quarantined tenant's bucket entered the reclaimable estate"

    # ...and a record pointing into it is refused end-to-end, not merely absent from a set. Driven over
    # a readable local root, because an s3:// root this process cannot reach would refuse for the
    # OTHER reason (unreadable manifest) and prove nothing about the quarantine rule.
    estate = _Estate(tmp_path)
    estate.put_record("q$table", location="s3://bkt-quarantined/uuid_q$table")
    out = _run(estate, data_roots={estate.data_root})
    assert out.purged == []
    assert "outside the maintained estate" in out.refused[0].reason


# --------------------------------------------------------------------------- #
# ordering: the record is ended only once its bytes are gone
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("uuid", "needle"),
    [
        pytest.param(_ORDERS_UUID, "could not decide", id="the-catalog-cannot-end-it"),
        # The record is filed under the uuid it keeps ([[LH-525]]), so one that keeps none is one no door can end.
        pytest.param("", "keeps no uuid", id="the-objects-uuid-cannot-be-read"),
    ],
)
def test_a_record_the_catalog_does_not_end_keeps_its_record(tmp_path: Path, doors: respx.MockRouter, uuid: str, needle: str) -> None:
    """An end the catalog does not make leaves the record, and the purge reports why rather than claiming a purge.

    The record is all that names what the object kept (its records, its claim, its base record), so it must survive for
    the next tick to end, whatever happened to the bytes before.
    """
    estate = _Estate(tmp_path)
    canonical, _ = estate.drop_recoverably("team", "orders", uuid=uuid)
    doors.routes["end"].mock(return_value=httpx.Response(503, json={"code": 17, "detail": "the store did not answer"}))

    out = _run(estate)

    assert (out.purged, [(r.kind, r.id) for r in out.refused]) == ([], [("table", canonical)])
    assert needle in out.refused[0].reason
    assert estate.record(canonical) is not None


def test_the_record_is_ended_only_once_its_bytes_are_gone(tmp_path: Path, doors: respx.MockRouter) -> None:
    """The ordering stated positively, so a swap fails even on the SUCCESS path: the end door probes the recorded path
    at the moment it is asked, and delete-then-end means it finds nothing there."""
    estate = _Estate(tmp_path)
    _, location = estate.drop_recoverably("team", "orders", uuid=_ORDERS_UUID)
    path = Path(location.removeprefix("file://"))
    bytes_at_the_end: list[bool] = []
    ended = end_door(estate.control_root, StorageOptions())

    def _probing(request: httpx.Request, kind: str, uuid: str) -> httpx.Response:
        bytes_at_the_end.append(path.exists())
        return ended(request, kind, uuid)

    doors.routes["end"].mock(side_effect=_probing)

    out = _run(estate)

    assert ([p.id for p in out.purged], bytes_at_the_end) == (["team$orders"], [False]), "the record was ended while its bytes were still there"


def test_an_expired_record_the_catalog_has_not_released_is_not_purged(tmp_path: Path, doors: respx.MockRouter) -> None:
    """The catalog vends a record's bytes session and ends it only once the release has run ([[XC-084]]): the settle an
    undrop admitted at the deadline needs, and the telling of each person who loses access, come first. So the purge
    leaves an expired record the catalog has not released for the tick that releases it, and asks no door of it."""
    estate = _Estate(tmp_path)
    _, location = estate.drop_recoverably("team", "orders", uuid=_ORDERS_UUID, released=False)

    out = _run(estate)

    asked = [route.name for route in doors.routes if route.called]
    assert (out.purged, out.refused, Path(location.removeprefix("file://")).exists(), asked) == ([], [], True, [])


# --------------------------------------------------------------------------- #
# the per-tick cap
# --------------------------------------------------------------------------- #


def test_the_purge_is_capped_per_tick_oldest_first(tmp_path: Path) -> None:
    """A backlog is drained over ticks, not turned into one unbounded delete storm.

    The remainder is REPORTED (``capped``), never silently dropped — a cap that reads as "that was all of
    them" is the same class of lie as a truncated scan reporting an estate clean. Oldest-first, so a
    capped run is reproducible rather than a lottery.
    """
    estate = _Estate(tmp_path)
    oldest, oldest_path = estate.drop_recoverably("team", "a", dropped_at=_NOW - timedelta(days=90))
    middle, middle_path = estate.drop_recoverably("team", "b", dropped_at=_NOW - timedelta(days=60))
    newest, newest_path = estate.drop_recoverably("team", "c", dropped_at=_NOW - timedelta(days=30))

    out = _run(estate, settings=_settings(tmp_path, trash_purge_max_per_tick=2))

    assert out.due == 3
    assert out.capped == 1
    assert [p.id for p in out.purged] == [oldest, middle]
    assert not Path(oldest_path.removeprefix("file://")).exists()
    assert not Path(middle_path.removeprefix("file://")).exists()
    assert Path(newest_path.removeprefix("file://")).is_dir(), "the capped record was purged anyway"
    assert estate.record(newest) is not None

    # ...and the next tick takes it, which is what makes the cap a pacing device rather than a ceiling.
    again = _run(estate, settings=_settings(tmp_path, trash_purge_max_per_tick=2))
    assert [p.id for p in again.purged] == [newest]
    assert again.capped == 0


# --------------------------------------------------------------------------- #
# the announcement
# --------------------------------------------------------------------------- #


def test_one_control_event_per_purged_record(tmp_path: Path) -> None:
    """A reclamation is a governance change, so it lands on the control stream — one event per record.

    The verbs are the PURGE's own — `table_purged` / `namespace_purged`, not the drop actions (diff2
    F10 item 6). Under `table_dropped` an automated reclamation reads as a person deleting the table to
    every consumer that does not read `extra.reason`: a governance feed shows a deletion with a service
    identity in the actor column. Two different
    facts — "someone decided to remove this" and "the grace period ran out" — differ in cause, in
    appealability, and in finality, so they get two names.

    ``extra`` stays a pointer payload (claim-check): the reason, the deadline, the size. ``reason`` is
    UNCHANGED and still says `trash_expired`, so a consumer keying on it keeps working — the verbs are
    purely additive.

    Each event names its object's uuid and its namespace's (ADR 0173 §10), both as the trash record keeps them from the
    drop: a namespace dropped since names no live row to read its uuid from.
    """
    estate = _Estate(tmp_path)
    canonical, _ = estate.drop_recoverably("team", "orders", uuid=_ORDERS_UUID, namespace_uuid=_TEAM_UUID)
    orphaned, _ = estate.drop_recoverably("gone", "t", uuid=_T_UUID, namespace_uuid=_GONE_UUID)
    estate.ns.drop_namespace(DropNamespaceRequest(id=["gone"]))
    estate.put_record("archive", uuid=_ARCHIVE_UUID, location="", kind="namespace", properties={"rask.uuid": _ARCHIVE_UUID})
    control = _RecordingControl()

    out = _run(estate, control=control)

    assert len(out.purged) == 3
    assert {(e.action, e.object_type, e.object_id, e.object_name, e.object_uuid, e.namespace_uuid) for e in control.events} == {
        ("table_purged", "table", f"table:{_ORDERS_UUID}", canonical, _ORDERS_UUID, _TEAM_UUID),
        ("table_purged", "table", f"table:{_T_UUID}", orphaned, _T_UUID, _GONE_UUID),
        ("namespace_purged", "namespace", f"namespace:{_ARCHIVE_UUID}", "archive", _ARCHIVE_UUID, None),
    }
    assert all(e.actor == "service:maintenance" for e in control.events)
    assert all(e.extra["reason"] == "trash_expired" for e in control.events)
    # Claim-check: pointers, never data.
    assert all(len(json.dumps(e.extra)) < 512 for e in control.events)


# --------------------------------------------------------------------------- #
# LH-095: a permanent exclusion must read as permanent
# --------------------------------------------------------------------------- #


def test_a_SECOND_refusal_counts_up_rather_than_looking_like_the_first(tmp_path: Path) -> None:
    """The whole point of the row: two refusals must be distinguishable from one.

    `attempts` accumulates and `last_refusal` is replaced, so the record answers both "how long has
    this been stuck" and "what is the current reason" — the second matters because a record can move
    between refusal causes (a store that answers again turns an unreadable location into a still-registered table).
    """
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders", dropped_at=datetime.now(UTC) - timedelta(days=30))
    estate.reregister("team", "orders", location=location)
    settings = _settings(tmp_path, trash_purge_enabled=True)

    _run(estate, settings=settings)
    out = _run(estate, settings=settings)

    record = estate.record(canonical)
    assert record is not None
    assert record.get("attempts") == 2, f"the second refusal did not count up: {record}"
    assert [r.attempts for r in out.refused] == [2], "the report does not carry the accumulated count"


def test_persisting_a_refusal_NEVER_turns_a_refusal_into_a_failure(tmp_path: Path, doors: respx.MockRouter) -> None:
    """`_purge_one` is documented "never raises; every failure is a refusal", and this must not break it.

    The annotation is bookkeeping ABOUT a refusal, strictly less important than the refusal itself. A
    store that refuses the write, a record another replica cleared in between, a lost CAS race — none
    of them may propagate, because doing so would convert a correctly-reported refusal into an
    unhandled error on the reclamation path.
    """

    doors.routes["refusals"].mock(return_value=httpx.Response(503, json={"code": 17, "detail": "the control root is unreachable"}))
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders", dropped_at=datetime.now(UTC) - timedelta(days=30))
    estate.reregister("team", "orders", location=location)

    out = _run(estate, settings=_settings(tmp_path, trash_purge_enabled=True))

    assert [r.id for r in out.refused] == [canonical], "a failed annotation swallowed the refusal itself"


# --------------------------------------------------------------------------- #
# the dry run: see what reclamation WOULD do, before spending the permission
# --------------------------------------------------------------------------- #


def test_a_dry_run_names_what_it_WOULD_purge_and_deletes_nothing(tmp_path: Path) -> None:
    """CONTRACT: with the dry run on and the purge OFF, the tick reports its plan and mutates nothing.

    The estate's rule is that a reclaimer earns its delete permission by first proving its report runs
    clean — but there was no way to SEE what spending that permission would cost. `enabled=false` lists
    nothing at all (it returns before the trash prefix is even read), so the only way to learn what the
    purge would do was to let it do it. That is the wrong order for the one irreversible operation this
    service performs.

    THE SAME CODE PATH, not a second implementation. The plan is produced by the real `_purge_one` with
    its real `check`, so what it reports is what would actually happen; a separate "preview" routine is
    a second mechanism that drifts from the one it is previewing, and the drift would be discovered by
    deleting the wrong thing.
    """
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders", dropped_at=datetime.now(UTC) - timedelta(days=30))
    before = _fingerprint(tmp_path)

    out = _run(estate, settings=_settings(tmp_path, trash_purge_enabled=False, trash_purge_dry_run=True))

    assert out.ran is True, f"the dry run never got past the gates: {out.reason}"
    assert out.dry_run is True
    assert out.due == 1
    assert [r.id for r in out.would_purge] == [canonical], f"the plan does not name the record: {out.would_purge}"
    assert out.purged == [], "a DRY run reported an actual purge"
    assert _fingerprint(tmp_path) == before, "the dry run mutated the estate"
    assert Path(location.removeprefix("file://")).is_dir(), "the dry run deleted the bytes"
    assert estate.record(canonical) is not None, "the dry run cleared the trash record"


def test_a_dry_run_does_not_write_the_refusal_MEMORY_either(tmp_path: Path) -> None:
    """The subtlest way a dry run stops being dry: the refusal annotation.

    A refusal persists `attempts` and `last_refusal` onto the trash record, which is a WRITE. A preview
    that inflates the attempt count changes the very evidence an operator is previewing — and does it on
    the records that are stuck, i.e. exactly the ones being inspected. The refusal is still REPORTED;
    only the memory of it is withheld.
    """
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders", dropped_at=datetime.now(UTC) - timedelta(days=30))
    estate.reregister("team", "orders", location=location)  # still registered: the refusal case

    out = _run(estate, settings=_settings(tmp_path, trash_purge_enabled=False, trash_purge_dry_run=True))

    assert [r.id for r in out.refused] == [canonical], "the dry run hid the refusal it was meant to preview"
    record = estate.record(canonical)
    assert record is not None
    assert "attempts" not in record, f"the dry run wrote the refusal memory: {record}"


def test_the_dry_run_reports_the_SAME_gate_that_would_block_a_real_purge(tmp_path: Path) -> None:
    """A preview that ignores the gates would promise reclamation the real purge refuses to perform.

    `report_is_clean` is the permission: on a drifting estate the purge does nothing, and that is the
    designed failure direction. The dry run must inherit it, so "it would purge nothing because the
    drift report is not clean" is a visible answer rather than an empty list that reads as "nothing to
    reclaim".
    """
    estate = _Estate(tmp_path)
    estate.drop_recoverably("team", "orders", dropped_at=datetime.now(UTC) - timedelta(days=30))
    drifting = _clean_report()
    drifting.counts["ghost_projects"] = 1
    drifting.total = 1

    out = _run(estate, settings=_settings(tmp_path, trash_purge_enabled=False, trash_purge_dry_run=True), report=drifting)

    assert out.ran is False
    assert "NOT clean" in (out.reason or ""), out.reason
    assert out.would_purge == []


def test_the_dry_run_NEVER_widens_a_real_purge(tmp_path: Path) -> None:
    """Both flags on must still be a real purge — the dry run may only ever SUBTRACT capability.

    Stated as a gate because the opposite wiring is an easy mistake with a severe cost: a deployment
    that means to reclaim and quietly previews forever looks identical to a healthy one, and the backlog
    it is not draining is exactly what nobody notices.
    """
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders", dropped_at=datetime.now(UTC) - timedelta(days=30))

    out = _run(estate, settings=_settings(tmp_path, trash_purge_enabled=True, trash_purge_dry_run=True))

    assert out.dry_run is True, "the dry run flag was ignored when the purge was also enabled"
    assert out.purged == [], "the dry run performed a real purge"
    assert [r.id for r in out.would_purge] == [canonical]
    assert Path(location.removeprefix("file://")).is_dir(), "the dry run deleted the bytes"


def test_a_dry_run_tick_is_DISTINGUISHABLE_on_the_reclamation_counters(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTRACT: every reclamation counter point says whether the tick that produced it was a preview.

    Two failures ride on this, and both are silent. A dry run still counts its refusals, so without a
    dimension a preview's refusals are indistinguishable from a real pass's on a dashboard — the tick
    reports reclamation activity while reclaiming nothing. And an operator who sets `trashPurge: true`
    while `trashPurgeDryRun` is still on gets a PERMANENT preview: the flag wins by design, and nothing
    else in the estate — no metric dimension, no alert rule, no dashboard panel — would ever say so.
    The backlog it silently never drains is exactly what nobody notices.

    Asserted on the attributes reaching the counters, because the point of the dimension is that a
    query can split on it.
    """
    from maintenance.core import metrics as metrics_mod

    seen: list[tuple[int, dict[str, object]]] = []

    class _Counter:
        def add(self, value: int, attributes: dict[str, object] | None = None) -> None:
            seen.append((value, dict(attributes or {})))

    for name in ("_trash_purged", "_trash_refused", "_trash_bytes", "_trash_planned"):
        monkeypatch.setattr(metrics_mod, name, _Counter(), raising=False)

    metrics_mod.record_trash_purge(purged_by_kind={}, refused_by_kind={"table": 2}, bytes_reclaimed=0, planned_by_kind={"table": 3}, dry_run=True)

    assert seen, "the counters recorded nothing at all"
    assert all("lance.maintenance.dry_run" in attrs for _v, attrs in seen), f"a point carries no dry-run dimension: {seen}"
    assert all(attrs["lance.maintenance.dry_run"] is True for _v, attrs in seen), seen
    assert any(v == 3 for v, _a in seen), f"the planned count never reached a counter: {seen}"


def test_the_purge_blocker_names_only_the_categories_that_actually_BLOCK() -> None:
    """CONTRACT: the reason names the categories inside `report.total`, never the non-gating ones.

    `report.total` deliberately excludes `NON_GATING_CATEGORIES` — `unbound_namespaces` — because it is not
    a storage fact, cannot change which bytes the purge touches, and crucially HAS NO DOOR: no endpoint in
    the product binds a legacy namespace. That exclusion exists because gating on it made the gate
    permanently unsatisfiable.

    The message did not inherit it. It listed every non-zero category, so an operator was pointed at
    work that cannot be done and that would not have unblocked the purge if it could. Measured on the
    live estate 2026-09-10:

        "NOT clean: 13 finding(s) across ['ghost_projects', 'orphan_buckets',
                                          'orphaned_annotation_tasks', 'unbound_namespaces']"

    13 = ghost_projects 1 + orphan_buckets 12. The other two contributed ZERO to the number they are
    listed beside — which is precisely the failure `NON_GATING_CATEGORIES`' own docstring names: "it
    hides the categories that DO matter behind a total that never falls." The total was fixed and the
    sentence an operator actually reads was not.
    """
    report = _clean_report()
    report.counts.update({"ghost_projects": 1, "orphan_buckets": 12, "unbound_namespaces": 4})
    report.total = 13  # what `reconcile` computes: the gating categories only

    reason = mod.report_is_clean(report)

    assert reason is not None
    assert "ghost_projects" in reason and "orphan_buckets" in reason, reason
    assert "unbound_namespaces" not in reason, f"the blocker names a non-gating category: {reason}"


def test_a_table_recovered_AFTER_the_liveness_snapshot_is_still_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE RACE the neighbouring test does not drive, and the one the docstring promises to close.

    `test_a_recovered_id_is_REFUSED` registers the table BEFORE the tick, so the once-per-tick
    `live_ids` snapshot already contains it and the refusal ladder catches it on the first read. That
    proves the ladder works; it says nothing about WHEN the answer was computed.

    The real window is narrower and is the one `undrop` opens: the snapshot is taken once, before the
    loop, and every record is then checked against it. An undrop landing after that snapshot — a
    `register_table` at the same location, or the catalog's own re-register in the instant before it
    clears the record — is invisible to every later record in the same tick. Purge then revokes a LIVE
    table's grants and deletes its bytes, and answers 200.

    Driven deterministically rather than by timing: the undrop is re-registered from inside the snapshot's own read,
    right after it returns, which lands it exactly in the window. Nothing here is a sleep or a thread.
    """
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders")

    real_liveness = mod.liveness_across
    reads: list[None] = []

    def _snapshot_then_undrop(*args: Any, **kwargs: Any) -> Any:
        result = real_liveness(*args, **kwargs)
        if not reads:
            estate.reregister("team", "orders", location=location)  # the undrop lands mid-run, after the snapshot
        reads.append(None)
        return result

    monkeypatch.setattr(mod, "liveness_across", _snapshot_then_undrop)

    out = _run(estate)

    # THE TEST'S OWN PRECONDITION. If the mid-tick re-register had failed, the id would not be live and
    # purging it would be CORRECT — the assertions below would then be demanding the wrong behaviour.
    live_now = mod.live_object_ids(estate.data_root, StorageOptions())
    assert live_now is not None and canonical in live_now, f"the mid-tick undrop did not land; this test proves nothing: {live_now}"

    assert out.purged == [], f"purge reclaimed a table that was re-registered during the tick: {out.purged}"
    assert [(r.kind, r.id) for r in out.refused] == [("table", canonical)], out.refused
    assert "still registered" in out.refused[0].reason, out.refused[0].reason
