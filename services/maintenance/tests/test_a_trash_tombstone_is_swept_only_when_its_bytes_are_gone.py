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
  estate's guards, for the reason `refuse_a_referring_datasets_source` gives — what it guards is
  unrecoverable.
"""

from functools import partial
from pathlib import Path
from uuid import uuid4

from lakehouse.records import location_claims
from lakehouse.records.base_registry import BaseOrigin, BaseRegistry, BaseRole, RecordedBase, claim_bases
from lakehouse.storage.options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.services import tombstones
from maintenance.services.outcomes import Failed
from maintenance.services.purge import held_by
from maintenance.services.reconcile import OrphanedTrash, ReconcileReport


_GONE = "s3://dead-wh/ns$t.lance"
_ALIVE = "s3://live-wh/ns$t.lance"
_UNREADABLE = "s3://blip-wh/ns$t.lance"
_GONE_UUID = "9d2b7e41-6c3a-4f58-b1e0-3a7c5d9f2e64"


def _report() -> ReconcileReport:
    return ReconcileReport(
        checked_at="2026-09-20T00:00:00Z",
        orphaned_trash=[
            OrphanedTrash(id="tr-gone", uuid=_GONE_UUID, kind="table", location=_GONE),
            OrphanedTrash(id="tr-alive", uuid="2f8a4c6e-1d3b-4e75-9a0c-6b5d7e3f1a28", kind="table", location=_ALIVE),
            OrphanedTrash(id="tr-blip", uuid="e6c3a1f9-7b24-4d08-8e5a-1c9f3b2d7a46", kind="table", location=_UNREADABLE),
        ],
    )


def _probe(finding: OrphanedTrash) -> int:
    """Object count at the finding's location; raises where the store cannot be read."""
    if finding.location == _UNREADABLE:
        raise OSError("registry blip")
    return 3 if finding.location == _ALIVE else 0


def _settings(**over: object) -> MaintenanceSettings:
    base: dict[str, object] = {"MAINTENANCE_TOMBSTONE_SWEEP_ENABLED": True, "MAINTENANCE_TOMBSTONE_SWEEP_DRY_RUN": True}
    return MaintenanceSettings.model_validate({**base, **over})


class _Deleter:
    """Records instead of writing to the registry — a class, not a lambda, so its type is the seam's.

    The seam takes the whole :class:`~maintenance.services.tombstones.Tombstone` rather than an id,
    because `trash.clear` is keyed on (uuid, KIND) ([[LH-525]]): several records can be dropped under one
    name, so only the uuid says which one is swept, and clearing under the wrong kind would leave the real
    one behind while reporting a sweep.
    """

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, record: tombstones.Tombstone) -> None:
        self.seen.append(record.uuid)


def test_a_record_whose_bytes_stand_only_in_its_data_directories_is_refused(tmp_path: Path) -> None:
    """The sweep's probe asks every place the catalog's end door judges ([[LH-286]]): the table's location, in both
    spellings, and each data directory its claim names on its table. A table whose fragments sit on a data base holds
    them there with its root already empty, and a record swept on the root alone strands them, or is refused by the
    door the sweep then asks."""
    control, options = f"file://{tmp_path / 'control'}", StorageOptions()
    location, directory = f"file://{tmp_path / 'gone' / 'ns$t'}", tmp_path / "base" / "t"
    registry = BaseRegistry(control_root=control, storage_options=options)
    claim_bases(registry, location, [RecordedBase(path=f"file://{directory}", role=BaseRole.DATA, origin=BaseOrigin.CREATE)])
    claimant = location_claims.DirectoryClaimant(uuid=_GONE_UUID, root=location, table="ns$t", segments=["ns", "t"])
    location_claims.take_directory(location_claims.ClaimStore(control_root=control), f"file://{directory}", claimant)
    directory.mkdir(parents=True)
    (directory / "0.lance").write_bytes(b"a fragment")
    finding = OrphanedTrash(id="ns$t", uuid=_GONE_UUID, kind="table", location=location)

    planned, refused = tombstones.plan_sweep(
        ReconcileReport(checked_at="2026-09-20T00:00:00Z", orphaned_trash=[finding]), probe=partial(held_by, storage_options=options, registry=registry)
    )

    assert (planned, [(one.kind, one.id, one.uuid) for one in refused]) == ([], [("table", "ns$t", _GONE_UUID)]), (planned, refused)
    assert "still holds 1 object" in refused[0].reason, refused


def test_a_dry_run_plans_and_deletes_nothing() -> None:
    deleter = _Deleter()
    out = tombstones.sweep_tombstones(_settings(), report=_report(), probe=_probe, delete=deleter)

    assert out.dry_run is True
    assert [(s.id, s.location) for s in out.swept] == [("tr-gone", _GONE)], "only the record whose bytes are gone is planned"
    assert deleter.seen == [], f"a dry run deleted {deleter.seen}"


def test_disabled_does_not_even_probe() -> None:
    probed: list[str] = []

    def _counting(finding: OrphanedTrash) -> int:
        probed.append(finding.location)
        return 0

    out = tombstones.sweep_tombstones(_settings(MAINTENANCE_TOMBSTONE_SWEEP_ENABLED=False), report=_report(), probe=_counting, delete=_Deleter())

    assert out.enabled is False
    assert out.swept == [] and probed == []


def test_armed_deletes_exactly_the_proven_tombstones() -> None:
    deleter = _Deleter()
    out = tombstones.sweep_tombstones(_settings(MAINTENANCE_TOMBSTONE_SWEEP_DRY_RUN=False), report=_report(), probe=_probe, delete=deleter)

    assert deleter.seen == [_GONE_UUID], f"deleted the wrong set: {deleter.seen}"
    assert out.dry_run is False and len(out.swept) == 1


def test_the_cap_truncates_and_says_it_did() -> None:
    many = ReconcileReport(
        checked_at="2026-09-20T00:00:00Z",
        orphaned_trash=[OrphanedTrash(id=f"tr-{i}", uuid=str(uuid4()), kind="table", location=f"s3://dead-{i}/t.lance") for i in range(5)],
    )

    out = tombstones.sweep_tombstones(_settings(MAINTENANCE_TOMBSTONE_SWEEP_MAX_PER_TICK=2), report=many, probe=lambda _finding: 0, delete=_Deleter())

    assert len(out.swept) == 2 and out.capped == 3


def _held_then_empty() -> list[OrphanedTrash]:
    """Two drops of one name whose places still hold bytes, listed ahead of a record whose place is empty."""
    return [
        OrphanedTrash(id="ns$t", uuid="held-1", kind="table", location=_ALIVE),
        OrphanedTrash(id="ns$t", uuid="held-2", kind="table", location=_ALIVE),
        OrphanedTrash(id="ns$u", uuid="empty", kind="table", location=_GONE),
    ]


def test_records_refused_for_good_hold_no_slot_from_one_run_to_the_next() -> None:
    settings = _settings(MAINTENANCE_TOMBSTONE_SWEEP_MAX_PER_TICK=2)
    runs = [ReconcileReport(checked_at=f"2026-11-26T0{hour}:00:00Z", orphaned_trash=_held_then_empty()) for hour in range(4)]

    swept = {s.uuid for report in runs for s in tombstones.sweep_tombstones(settings, report=report, probe=_probe, delete=_Deleter()).swept}

    assert swept == {"empty"}


def test_an_audit_that_fires_daily_takes_the_next_share_each_day() -> None:
    settings = _settings(MAINTENANCE_TOMBSTONE_SWEEP_MAX_PER_TICK=2, MAINTENANCE_AUDIT_SCHEDULE="@daily")
    probed: set[str] = set()

    def _probing(finding: OrphanedTrash) -> int:
        probed.add(finding.uuid)
        return _probe(finding)

    for day in (26, 27):
        report = ReconcileReport(checked_at=f"2026-11-{day}T00:00:00Z", orphaned_trash=_held_then_empty())
        tombstones.sweep_tombstones(settings, report=report, probe=_probing, delete=_Deleter())

    assert probed == {"held-1", "held-2", "empty"}


def test_two_drops_of_one_name_are_each_refused_by_their_own_record() -> None:
    report = ReconcileReport(checked_at="2026-11-26T00:00:00Z", orphaned_trash=_held_then_empty())

    out = tombstones.sweep_tombstones(_settings(), report=report, probe=_probe, delete=_Deleter())

    assert sorted(one.uuid or "" for one in out.refused) == ["held-1", "held-2"]


def test_every_record_whose_end_fails_is_named_with_its_failure() -> None:
    report = ReconcileReport(
        checked_at="2026-11-26T00:00:00Z",
        orphaned_trash=[OrphanedTrash(id="ns$t", uuid=uuid, kind="table", location=_GONE) for uuid in ("first", "second")],
    )

    def _failing(record: tombstones.Tombstone) -> None:
        raise OSError(f"the door did not answer for {record.uuid}")

    out = tombstones.sweep_tombstones(_settings(MAINTENANCE_TOMBSTONE_SWEEP_DRY_RUN=False), report=report, probe=_probe, delete=_failing)

    assert out.failed == [Failed(kind="table", id="ns$t", uuid=uuid, error=f"OSError: the door did not answer for {uuid}") for uuid in ("first", "second")]
    assert out.swept == []
