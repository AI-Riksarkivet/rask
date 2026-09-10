"""The expired-trash purge (#79) — reclamation, against REAL stores.

This is the first thing in ``services/maintenance`` that deletes bytes it did not write, so the harness
is deliberately the same shape as ``test_drop_protection.py`` / ``test_reconcile_report.py``: a real
``file://`` control root written with the real ``service_kit.lakehouse.trash`` primitives, and a real
Lance estate built through ``lance_namespace.connect("dir", …)`` — the impl the chart runs. Nothing about
the object store or the trash registry is mocked, because every interesting refusal in this module is a
statement about what is REALLY on disk, and a double would happily agree with a wrong one.

The recoverable drop is reproduced faithfully rather than simulated: a table is created through the
backend and then ``deregister_table``-d, which is exactly what the catalog does when a grace period is
configured — the manifest row goes away, the bytes stay. A trash record over those bytes is therefore the
real thing, not a fixture shaped like one.

OpenFGA is the one seam that is faked, and only at ``fga.revoke_object_tuples``: there is no in-process
OpenFGA server, and the alternative (asserting nothing about revocation) would leave the load-bearing
ordering rule — revoke BEFORE delete — unproven.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pyarrow as pa
import pytest
from lance_namespace import (
    CreateNamespaceRequest,
    CreateTableRequest,
    DeregisterTableRequest,
    DescribeTableRequest,
    ServiceUnavailableError,
    connect,
)
from pydantic import SecretStr

from maintenance.core.config import MaintenanceSettings
from maintenance.services import purge as mod
from maintenance.services.reconcile import CATEGORIES, CategorySkipped, CategoryUnavailable, IncompleteScan, ReconcileReport
from service_kit.control_events import CatalogControlEvent
from service_kit.governed import fga as fga_module
from service_kit.lakehouse import trash
from service_kit.lakehouse.base_refs import BaseRefs, normalise


_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


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
    }
    return MaintenanceSettings(**{**base, **over})


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

    def drop_recoverably(self, *segments: str, grace_days: int = 7, dropped_at: datetime | None = None) -> tuple[str, str]:
        """The catalog's recoverable drop, for real: deregister (bytes stay) + file a trash record.

        Returns ``(canonical_id, location)``. ``dropped_at`` in the past is how a record becomes EXPIRED.
        """
        location = self.create_table(*segments)
        self.ns.deregister_table(DeregisterTableRequest(id=list(segments)))
        canonical = "$".join(segments)
        self.put_record(canonical, location=location, grace_days=grace_days, dropped_at=dropped_at)
        return canonical, location

    def put_record(
        self,
        canonical: str,
        *,
        location: str,
        grace_days: int = 7,
        dropped_at: datetime | None = None,
        kind: str = "table",
    ) -> dict[str, Any]:
        record = trash.make_record(
            canonical,
            location=location,
            dropped_by="user:alice",
            grace_days=grace_days,
            now=dropped_at or (_NOW - timedelta(days=30)),
            kind=kind,
        )
        trash.put(self.control_root, {}, record)
        return record

    def record(self, canonical: str, *, kind: str = "table") -> dict[str, Any] | None:
        return trash.get(self.control_root, {}, canonical, kind=kind)


class _Revoker:
    """The FGA revoke seam. Records every call and, crucially, what was ON DISK when it was made."""

    def __init__(self, *, error: Exception | None = None, count: int = 2, probe: Callable[[], bool] | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.bytes_present_at_call: list[bool] = []
        self.error = error
        self.count = count
        self.probe = probe

    async def __call__(self, _client: object, obj: str, *, actor: str, origin: str, **_kw: object) -> list[SimpleNamespace]:
        """Hands back WHAT it revoked, mirroring the helper — `count` still sets how many, since these
        tests are about ORDERING (revoke before delete), not about who held the grants."""
        self.calls.append((obj, actor, origin))
        if self.probe is not None:
            self.bytes_present_at_call.append(self.probe())
        if self.error is not None:
            raise self.error
        return [SimpleNamespace(user=f"user:u{i}", relation="owner", object=obj) for i in range(self.count)]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fga_module, "revoke_object_tuples", self)


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
    fga_client: object | None = None,
    control: _RecordingControl | None = None,
    data_roots: set[str] | None = None,
) -> mod.TrashPurgeReport:
    return asyncio.run(
        mod.purge_expired_trash(
            settings or _settings(estate.tmp_path),
            report=report or _clean_report(),
            fga_client=fga_client,
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


def test_a_skipped_category_does_not_block_but_is_named(tmp_path: Path) -> None:
    """A deliberate skip is not drift — but the purge may not pretend the estate was fully verified.

    The shipped config skips ``orphan_files`` (it opens every dataset), so blocking on a skip would make
    reclamation unreachable in every real deployment. The skips are copied onto the purge report instead,
    so an operator reading it can see exactly which part of the estate nobody looked at.
    """
    estate = _Estate(tmp_path)
    estate.drop_recoverably("team", "orders")

    out = _run(estate, report=_clean_report(skipped=("orphan_files", "unbound_namespaces")))

    assert out.ran is True
    assert out.skipped_categories == ["orphan_files", "unbound_namespaces"]
    assert len(out.purged) == 1


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #


def test_an_expired_table_record_deletes_its_recorded_path_and_clears_the_record(tmp_path: Path) -> None:
    """The point of the whole feature: expired bytes go away, and the record goes with them.

    The record is cleared LAST so any failure above leaves it for the next tick — asserting it is gone is
    therefore also asserting the whole sequence completed.
    """
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders")
    path = Path(location.removeprefix("file://"))
    assert path.is_dir(), "the fixture must really have bytes, or 'deleted' proves nothing"

    out = _run(estate)

    assert out.ran is True
    assert out.due == 1
    assert out.refused == []
    assert [(p.kind, p.id) for p in out.purged] == [("table", canonical)]
    assert out.purged[0].bytes_deleted > 0
    assert out.purged[0].files_deleted > 0
    assert not path.exists(), "the recorded path survived the purge"
    assert estate.record(canonical) is None, "the trash record survived its own purge"


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


def test_a_namespace_record_clears_and_revokes_but_deletes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#96: a recoverable CASCADE trashes namespaces too, and a namespace owns no bytes.

    Its record carries ``location=""`` — a manifest ROW is all there ever was — so the purge revokes and
    clears and touches the data root not at all. ``trash.clear`` must be called with ``kind="namespace"``
    or it hashes the wrong key, silently no-ops, and the record re-purges every tick forever.
    """
    estate = _Estate(tmp_path)
    estate.create_table("team", "orders")  # a live sibling, so "nothing deleted" is a real claim
    revoker = _Revoker()
    revoker.install(monkeypatch)
    estate.put_record("archive", location="", kind="namespace")
    before = _fingerprint(estate.data)

    out = _run(estate, settings=_settings(tmp_path, fga_enabled=True), fga_client=object())

    assert [(p.kind, p.id, p.location) for p in out.purged] == [("namespace", "archive", "")]
    assert out.purged[0].bytes_deleted == 0
    assert _fingerprint(estate.data) == before, "a namespace purge touched the data root"
    assert estate.record("archive", kind="namespace") is None
    assert revoker.calls == [("namespace:archive", "service:maintenance", "lifecycle_delete")]


# --------------------------------------------------------------------------- #
# the refusal ladder
# --------------------------------------------------------------------------- #


def test_a_recovered_id_is_REFUSED(tmp_path: Path) -> None:
    """THE undrop-crash window, and the reason liveness is re-checked immediately before deleting.

    ``undrop`` re-registers the table and THEN clears the trash record. A crash in between — or anyone
    running ``register_table`` at the same location — leaves a LIVE table with a stale record pointing at
    its bytes. Purging that record deletes a live table. Here the id is genuinely back in ``__manifest``
    (created through the real backend), so the purge must refuse and leave both the bytes and the record.
    """
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders")
    recovered = estate.create_table("team", "orders")  # the undrop half that DID land
    path = Path(recovered.removeprefix("file://"))
    assert path.is_dir()

    out = _run(estate)

    assert out.purged == []
    assert [(r.kind, r.id) for r in out.refused] == [("table", canonical)]
    assert "still registered" in out.refused[0].reason
    assert path.is_dir(), "the purge deleted a LIVE table's bytes"
    assert estate.record(canonical) is not None
    assert Path(location.removeprefix("file://")).exists() or path.is_dir()


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
    """
    estate = _Estate(tmp_path)
    estate.create_table("team", "orders")
    before = _fingerprint(estate.data)
    estate.put_record("evil_manifest", location=f"{estate.data_root}/__manifest")
    estate.put_record("evil_nested", location=f"{estate.data_root}/_trash/some-record.json")

    out = _run(estate)

    assert out.purged == []
    assert {r.id for r in out.refused} == {"evil_manifest", "evil_nested"}
    assert all("control prefix" in r.reason for r in out.refused)
    assert _fingerprint(estate.data) == before


def test_a_deactivated_warehouses_bucket_is_outside_the_maintained_estate(tmp_path: Path) -> None:
    """Deactivate is offboarding step one: the resolver already 403s every operation on that tenant.

    Reclaiming its dropped tables would be the one process still destroying data the estate has said
    nobody may touch — and unlike the orphan REPORT (which deliberately still scans a quarantined
    warehouse, because naming findings changes nothing on disk), this one deletes.
    """
    from catalog.services import warehouses as wh_svc

    control_root = f"file://{tmp_path / 'control'}"
    wh_svc.put_warehouse(control_root, {}, {"id": "wh-live", "bucket": "bkt-live", "root_uri": "s3://bkt-live", "project": "acme", "created_at": "t"})
    wh_svc.put_warehouse(
        control_root,
        {},
        {"id": "wh-gone", "bucket": "bkt-quarantined", "root_uri": "s3://bkt-quarantined", "project": "acme", "created_at": "t", "status": "deactivated"},
    )
    settings = _settings(tmp_path)

    roots = mod.maintained_roots(settings, {})

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
# ordering: grants must never outlive the bytes
# --------------------------------------------------------------------------- #


def test_a_failed_revoke_leaves_the_bytes_and_the_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoke we could not perform is a reason NOT to delete.

    If the delete ran first, this test finds an empty directory: the bytes would be gone and the grants
    would still be live, with nothing left to retry against. Because the revoke runs first, the failure
    is fully recoverable — the record survives and the next tick tries the whole thing again.
    """
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders")
    path = Path(location.removeprefix("file://"))
    _Revoker(error=ServiceUnavailableError("openfga down")).install(monkeypatch)

    out = _run(estate, settings=_settings(tmp_path, fga_enabled=True), fga_client=object())

    assert out.purged == []
    assert [(r.kind, r.id) for r in out.refused] == [("table", canonical)]
    assert "revoke failed" in out.refused[0].reason
    assert path.is_dir(), "the bytes were deleted despite a failed revoke — grants now outlive them"
    assert estate.record(canonical) is not None


def test_the_revoke_happens_while_the_bytes_are_still_there(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordering stated positively, so a swap fails even on the SUCCESS path.

    The revoke seam probes the recorded path at the moment it is called. Revoke-then-delete means it sees
    the bytes; delete-then-revoke means it does not.
    """
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders")
    path = Path(location.removeprefix("file://"))
    revoker = _Revoker(count=3, probe=path.is_dir)
    revoker.install(monkeypatch)

    out = _run(estate, settings=_settings(tmp_path, fga_enabled=True), fga_client=object())

    assert revoker.calls == [(f"table:{canonical}", "service:maintenance", "lifecycle_delete")]
    assert revoker.bytes_present_at_call == [True], "the revoke ran AFTER the delete — grants outlived the bytes"
    assert out.purged[0].tuples_revoked == 3
    assert not path.exists()


def test_fga_enabled_without_a_client_purges_nothing(tmp_path: Path) -> None:
    """Enabled-but-unwired is a misconfiguration, and the safe answer is to do nothing at all.

    Running would mean deleting bytes while their grants stay live — the exact state the revoke-first
    rule exists to prevent, arrived at by omission instead of by failure.
    """
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders")

    out = _run(estate, settings=_settings(tmp_path, fga_enabled=True), fga_client=None)

    assert out.ran is False
    assert "no client is wired" in (out.reason or "")
    assert Path(location.removeprefix("file://")).is_dir()
    assert estate.record(canonical) is not None


def test_fga_disabled_skips_revocation_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With FGA off estate-wide there are no tuples to revoke — skip, and say zero rather than pretend."""
    estate = _Estate(tmp_path)
    canonical, _ = estate.drop_recoverably("team", "orders")
    revoker = _Revoker()
    revoker.install(monkeypatch)

    out = _run(estate, settings=_settings(tmp_path, fga_enabled=False))

    assert revoker.calls == [], "the purge called OpenFGA with FGA disabled"
    assert [(p.id, p.tuples_revoked) for p in out.purged] == [(canonical, 0)]


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
    F10 item 6). Reusing `table_dropped` made an automated reclamation indistinguishable from a person
    deleting the table unless a consumer read `extra.reason`, and the console's governance feed does
    not: it rendered a purge as a deletion with a service identity in the actor column. Two different
    facts — "someone decided to remove this" and "the grace period ran out" — differ in cause, in
    appealability, and in finality, so they get two names.

    ``extra`` stays a pointer payload (claim-check): the reason, the deadline, the size. ``reason`` is
    UNCHANGED and still says `trash_expired`, so a consumer keying on it keeps working — the verbs are
    purely additive.
    """
    estate = _Estate(tmp_path)
    canonical, _ = estate.drop_recoverably("team", "orders")
    estate.put_record("archive", location="", kind="namespace")
    control = _RecordingControl()

    out = _run(estate, control=control)

    assert len(out.purged) == 2
    assert {(e.action, e.object_type, e.object_id) for e in control.events} == {
        ("table_purged", "table", f"table:{canonical}"),
        ("namespace_purged", "namespace", "namespace:archive"),
    }
    assert all(e.actor == "service:maintenance" for e in control.events)
    assert all(e.extra["reason"] == "trash_expired" for e in control.events)
    # Claim-check: pointers, never data.
    assert all(len(json.dumps(e.extra)) < 512 for e in control.events)


def test_a_refused_record_announces_nothing(tmp_path: Path) -> None:
    """Only a change that HAPPENED is announced — the catalog's own rule, and it matters more here,
    because a purge event a console acts on for bytes that still exist is a lie about the estate."""
    estate = _Estate(tmp_path)
    estate.drop_recoverably("team", "orders")
    estate.create_table("team", "orders")  # recovered → refused
    control = _RecordingControl()

    out = _run(estate, control=control)

    assert out.refused and out.purged == []
    assert control.events == []


# --------------------------------------------------------------------------- #
# the selection rule is SHARED with the sweep's report-only log
# --------------------------------------------------------------------------- #


def test_the_sweep_and_the_purge_select_expired_trash_through_one_rule(tmp_path: Path) -> None:
    """The set the sweep NAMES and the set the purge DELETES must be the same set.

    Two copies of "what is expired" is how a report certifies one thing and a reclaimer acts on another.
    ``sweep.py`` calls ``purge.due_records`` for exactly this reason.
    """
    from maintenance.services import sweep

    assert sweep.purge.due_records is mod.due_records

    estate = _Estate(tmp_path)
    estate.drop_recoverably("team", "old", dropped_at=_NOW - timedelta(days=90))
    estate.drop_recoverably("team", "fresh", dropped_at=datetime.now(UTC))

    due = mod.due_records(estate.control_root, {})

    assert [r["id"] for r in due] == ["team$old"]


# --------------------------------------------------------------------------- #
# LH-095: a permanent exclusion must read as permanent
# --------------------------------------------------------------------------- #


def test_a_refused_record_REMEMBERS_that_it_was_refused(tmp_path: Path) -> None:
    """CONTRACT: a refusal is written onto the trash record, so the NEXT tick can see it happened.

    The purge reports refusals per tick through `RefusedRecord` and carried nothing across ticks, so a
    record refused every five minutes for thirty days read exactly like one refused once — and a
    PERMANENT exclusion, which is the state an operator actually has to act on, was invisible as
    permanent. Nothing in the estate could distinguish "this retried and will succeed" from "this will
    never succeed until a human intervenes".

    The record is still registered here, which is the commonest refusal and the one that is genuinely
    permanent until someone drops the live table again.
    """
    estate = _Estate(tmp_path)
    canonical, _ = estate.drop_recoverably("team", "orders", dropped_at=datetime.now(UTC) - timedelta(days=30))
    estate.create_table("team", "orders")  # re-registered since the drop: the bytes are LIVE

    out = _run(estate, settings=_settings(tmp_path, trash_purge_enabled=True))

    assert [r.id for r in out.refused] == [canonical], f"the record was not refused: {out.refused}"
    record = estate.record(canonical)
    assert record is not None, "the refusal destroyed the record it was refusing to act on"
    assert record.get("attempts") == 1, f"the refusal was not persisted: {record}"
    assert "still registered" in str((record.get("last_refusal") or {}).get("reason", "")), record


def test_a_SECOND_refusal_counts_up_rather_than_looking_like_the_first(tmp_path: Path) -> None:
    """The whole point of the row: two refusals must be distinguishable from one.

    `attempts` accumulates and `last_refusal` is replaced, so the record answers both "how long has
    this been stuck" and "what is the current reason" — the second matters because a record can move
    between refusal causes (a protected base is dropped, and it becomes still-registered instead).
    """
    estate = _Estate(tmp_path)
    canonical, _ = estate.drop_recoverably("team", "orders", dropped_at=datetime.now(UTC) - timedelta(days=30))
    estate.create_table("team", "orders")
    settings = _settings(tmp_path, trash_purge_enabled=True)

    _run(estate, settings=settings)
    out = _run(estate, settings=settings)

    record = estate.record(canonical)
    assert record is not None
    assert record.get("attempts") == 2, f"the second refusal did not count up: {record}"
    assert [r.attempts for r in out.refused] == [2], "the report does not carry the accumulated count"


def test_persisting_a_refusal_NEVER_turns_a_refusal_into_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_purge_one` is documented "never raises; every failure is a refusal", and this must not break it.

    The annotation is bookkeeping ABOUT a refusal, strictly less important than the refusal itself. A
    store that refuses the write, a record another replica cleared in between, a lost CAS race — none
    of them may propagate, because doing so would convert a correctly-reported refusal into an
    unhandled error on the reclamation path.
    """

    def _boom(*_a: object, **_kw: object) -> None:
        raise OSError("the control root is unreachable")

    monkeypatch.setattr(trash, "note_refusal", _boom)
    estate = _Estate(tmp_path)
    canonical, _ = estate.drop_recoverably("team", "orders", dropped_at=datetime.now(UTC) - timedelta(days=30))
    estate.create_table("team", "orders")

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
    canonical, _ = estate.drop_recoverably("team", "orders", dropped_at=datetime.now(UTC) - timedelta(days=30))
    estate.create_table("team", "orders")  # still registered: the refusal case

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


def test_the_protection_prepass_scans_AS_DEEP_AS_the_thing_it_protects_against(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTRACT: the shallow-clone pre-pass walks to the SAME depth the rest of maintenance walks.

    `_estate_base_refs` called `discover_datasets(fs, bucket)` with the default bound while the sweep and
    the drift report both pass `settings.discovery_max_depth`. `discoveryMaxDepth` is the documented
    lever for reaching datasets nested deeper than three levels — so raising it widened what the purge
    may DELETE while leaving what PROTECTS it at three.

    A referring clone below the shorter bound is never discovered, `is_protected` answers None for its
    source, and the purge deletes bytes a live dataset resolves through. The failure is silent and the
    data is gone — and nothing tells an operator that the protection floor stayed put when they raised
    the lever.
    """
    seen: dict[str, Any] = {}

    def _fake_discover(_fs: Any, bucket: str, **kw: Any) -> Any:
        seen["max_depth"] = kw.get("max_depth")
        return SimpleNamespace(uris=[], truncated=[])

    from maintenance.services import optimize as optimize_mod
    from service_kit.lakehouse import base_refs as base_refs_mod

    monkeypatch.setattr(optimize_mod, "discover_datasets", _fake_discover)
    monkeypatch.setattr(base_refs_mod, "protected_roots", lambda *_a, **_kw: BaseRefs())

    mod._estate_base_refs({"file:///nowhere"}, {}, max_depth=7)

    assert seen["max_depth"] == 7, f"the protection pre-pass walked to {seen['max_depth']} while the purge reaches 7"


def test_a_dry_run_APPLIES_the_shallow_clone_guard_it_already_paid_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTRACT: a previewed record whose bytes back a live clone is REFUSED in the preview, not planned.

    `_estate_base_refs` runs in full on a preview tick — it opens every dataset in every maintained
    bucket — and `BaseRefs.is_protected` is then a pure in-memory prefix compare. Leaving that answer
    unread meant the dry run paid the estate's most expensive read pass and discarded its result, while
    the values file and this module both told the operator the shallow-clone pre-pass was part of what
    the preview applies.

    The omission fails in the dangerous direction: `would_purge` would name a record a real run
    refuses, so the preview OVERSTATES reclamation on exactly the records where over-promising costs
    most — the ones whose deletion breaks a live dataset.
    """
    estate = _Estate(tmp_path)
    canonical, location = estate.drop_recoverably("team", "orders", dropped_at=datetime.now(UTC) - timedelta(days=30))
    # Through the estate's OWN comparator: `BaseRefs.protected` holds normalised roots, and
    # re-spelling one by hand here is precisely the mistake `normalise`'s docstring names — a guard
    # that silently never matches looks exactly like having no guard at all.
    guard = BaseRefs(protected={normalise(location)})
    monkeypatch.setattr(mod, "_estate_base_refs", lambda *_a, **_kw: guard)

    out = _run(estate, settings=_settings(tmp_path, trash_purge_enabled=False, trash_purge_dry_run=True))

    assert out.would_purge == [], f"the preview planned to reclaim a protected base: {out.would_purge}"
    assert [r.id for r in out.refused] == [canonical]
    assert "clone" in out.refused[0].reason.lower() or "base" in out.refused[0].reason.lower(), out.refused[0].reason


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
