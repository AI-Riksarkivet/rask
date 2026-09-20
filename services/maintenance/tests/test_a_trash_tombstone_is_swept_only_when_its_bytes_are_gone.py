"""An orphaned trash record is removed only after PROVING the location it names holds nothing.

[[LH-061]]'s storage tier. `repair.py` handles the authz tier and REFUSES `orphaned_trash` by name,
with the right reason: a trash record is the only remaining POINTER to the bytes it describes, so
dropping it blind strands them silently. That refusal is not overturned here — it is satisfied. This
pass deletes no bytes at all; it removes a record only where the bytes are already gone, which turns
"the only pointer" objection into a measurement instead of an assumption.

WHY THE MEASUREMENT IS WORTH MAKING, on this estate today: all 989 orphaned trash records point at a
bucket that holds nothing (measured in-pod 2026-09-20 across 63 buckets — 62 empty or absent, and the
one that still holds objects is a LIVE warehouse whose records are not orphaned at all). Their bytes
went with the deleted warehouse's bucket. What is left is a promise of recovery the estate can no
longer perform and a purge slot it can never use.

THE REFUSAL LEGS ARE THE POINT, and both are asserted rather than assumed from today's zero:

* a location that still HOLDS objects is refused and named — deleting it is the stranding this row
  exists to avoid, and today's 989/989 is not tomorrow's;
* a probe that FAILS is refused too. "We could not tell" reads as the refusal everywhere else in this
  estate's guards, for the reason `_refuse_a_referring_datasets_source` gives — what it guards is
  unrecoverable.
"""

from __future__ import annotations

from maintenance.core.config import MaintenanceSettings
from maintenance.services import tombstones
from maintenance.services.reconcile import OrphanedTrash, ReconcileReport


_GONE = "s3://dead-wh/ns$t.lance"
_ALIVE = "s3://live-wh/ns$t.lance"
_UNREADABLE = "s3://blip-wh/ns$t.lance"


def _report() -> ReconcileReport:
    return ReconcileReport(
        checked_at="2026-09-20T00:00:00Z",
        orphaned_trash=[
            OrphanedTrash(id="tr-gone", kind="table", location=_GONE),
            OrphanedTrash(id="tr-alive", kind="table", location=_ALIVE),
            OrphanedTrash(id="tr-blip", kind="table", location=_UNREADABLE),
        ],
    )


def _probe(location: str) -> int:
    """Object count at `location`; raises where the store cannot be read."""
    if location == _UNREADABLE:
        raise OSError("registry blip")
    return 3 if location == _ALIVE else 0


def _settings(**over: object) -> MaintenanceSettings:
    base: dict[str, object] = {"MAINTENANCE_TOMBSTONE_SWEEP_ENABLED": True, "MAINTENANCE_TOMBSTONE_SWEEP_DRY_RUN": True}
    return MaintenanceSettings.model_validate({**base, **over})


class _Deleter:
    """Records instead of writing to the registry — a class, not a lambda, so its type is the seam's.

    The seam takes the whole :class:`~maintenance.services.tombstones.Tombstone` rather than an id,
    because `trash.clear` is keyed on (id, KIND): a namespace record and a table record can share an
    id, and clearing the wrong kind would leave the real one behind while reporting a sweep.
    """

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, record: tombstones.Tombstone) -> None:
        self.seen.append(record.id)


def test_a_record_whose_BYTES_ARE_GONE_is_planned() -> None:
    planned, _refused = tombstones.plan_sweep(_report(), probe=_probe)

    assert [p.id for p in planned] == ["tr-gone"]
    assert planned[0].location == _GONE


def test_a_record_whose_bytes_STILL_EXIST_is_refused_and_named() -> None:
    """THE DEFECT THIS SUITE EXISTS FOR: the record is the only remaining pointer to those objects."""
    planned, refused = tombstones.plan_sweep(_report(), probe=_probe)

    assert "tr-alive" not in [p.id for p in planned], "a record naming live bytes was planned for deletion"
    assert "tr-alive" in refused
    assert "3" in refused["tr-alive"], f"the refusal does not say how many objects are there: {refused['tr-alive']}"


def test_an_UNREADABLE_location_is_refused_not_swept() -> None:
    """ "We could not tell" is the refusal, because what this guards is unrecoverable."""
    planned, refused = tombstones.plan_sweep(_report(), probe=_probe)

    assert "tr-blip" not in [p.id for p in planned]
    assert "tr-blip" in refused


def test_every_planned_sweep_RECORDS_the_probe_that_justified_it() -> None:
    """An audit line saying a reconciler deleted 50 records is unreadable without the evidence."""
    planned, _ = tombstones.plan_sweep(_report(), probe=_probe)

    assert all(p.probed_objects == 0 for p in planned), [p.model_dump() for p in planned]


def test_a_DRY_RUN_plans_and_deletes_NOTHING() -> None:
    deleter = _Deleter()
    out = tombstones.sweep_tombstones(_settings(), report=_report(), probe=_probe, delete=deleter)

    assert out.dry_run is True
    assert [s.id for s in out.swept] == ["tr-gone"]
    assert deleter.seen == [], f"a dry run deleted {deleter.seen}"


def test_DISABLED_does_not_even_probe() -> None:
    probed: list[str] = []

    def _counting(location: str) -> int:
        probed.append(location)
        return 0

    out = tombstones.sweep_tombstones(_settings(MAINTENANCE_TOMBSTONE_SWEEP_ENABLED=False), report=_report(), probe=_counting, delete=_Deleter())

    assert out.enabled is False
    assert out.swept == [] and probed == []


def test_ARMED_deletes_exactly_the_proven_tombstones() -> None:
    deleter = _Deleter()
    out = tombstones.sweep_tombstones(_settings(MAINTENANCE_TOMBSTONE_SWEEP_DRY_RUN=False), report=_report(), probe=_probe, delete=deleter)

    assert deleter.seen == ["tr-gone"], f"deleted the wrong set: {deleter.seen}"
    assert out.dry_run is False and len(out.swept) == 1


def test_the_CAP_truncates_and_SAYS_it_did() -> None:
    many = ReconcileReport(
        checked_at="2026-09-20T00:00:00Z",
        orphaned_trash=[OrphanedTrash(id=f"tr-{i}", kind="table", location=f"s3://dead-{i}/t.lance") for i in range(5)],
    )

    out = tombstones.sweep_tombstones(_settings(MAINTENANCE_TOMBSTONE_SWEEP_MAX_PER_TICK=2), report=many, probe=lambda _l: 0, delete=_Deleter())

    assert len(out.swept) == 2 and out.capped == 3


def test_a_CLEAN_report_sweeps_nothing() -> None:
    """The control. A pass that swept on an empty report would satisfy every leg above."""
    planned, refused = tombstones.plan_sweep(ReconcileReport(checked_at="2026-09-20T00:00:00Z"), probe=_probe)

    assert planned == [] and refused == {}
