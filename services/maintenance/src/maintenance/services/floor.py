"""Raising a dataset's LISTING FLOOR so Lance can reclaim residue it has gone blind to ([[LH-094]]).

**This module deletes nothing, and that is the whole design.** It writes ONE metadata-only commit to a
dataset whose orphans sit above its listing floor, which moves the floor above them; the DELETE is then
`cleanup_old_versions`'s on the next ordinary sweep, under Lance's own rule. So the orphan half of
reclamation stays what `purge.py` says it is — report-only, never a path this service names and removes.

WHY A FLOOR EXISTS AT ALL. `cleanup_old_versions` clamps its unreferenced-file listing to the commit
timestamp of the EARLIEST RETAINED MANIFEST (`rust/lance/src/dataset/cleanup.rs:332-341`, applied to
`_versions/`, `_transactions/`, `data/` and `_deletions/` at `:721-731`) BEFORE the 7-day unverified rule
at `:345` filters what was listed. `older_than` never enters that cutoff. Measured against pylance
11.0.0 with `delete_unverified=True` bypassing the age filter, the flip is at the manifest timestamp to
the second: -3600/-60/-1/0 s REMOVED, +1/+60/+3600 s KEPT.

SO A FILE ABOVE THE FLOOR IS PERMANENT, not late. A dataset collapsed to one live version holds every
file written after that surviving commit forever, and nothing Lance offers will enumerate it. That is
not a pathological case: **any** restore, re-upload or lifecycle rehydrate rewrites object mtimes above
the manifest timestamps that reference them and recreates it wholesale. Measured on the deployed estate
2026-09-19 — the MinIO PVCs were created `2026-09-11T10:50:09Z` while
`m2proof_silver$m2-proof-1788537252` embeds epoch `2026-09-04T15:54:12Z`, so every object was
re-uploaded a week after the commit that references it and the whole dataset sat above its own floor.

THE COMMIT IS A REAL VERSION AND ITS COST IS ACCEPTED, not hidden (owner ruling 2026-09-19,
`docs/DECISIONS.md`): the dataset gains a version representing no data change, written for a GC side
effect. The alternative was a one-off deleter over exactly what the scan names, which would have rask
deleting bytes on a live estate against a floor rule it derived itself — and both datasets in the
measured case are LIVE, so their residue is superseded-version material inside governed tables rather
than junk. The asymmetry `base_refs` already states decided it: a wrong refusal costs disk, a wrong
permit costs data.

THE LINEAGE PLANE ALREADY ANTICIPATES THIS SHAPE. Lance records the operation as `UpdateConfig`, which
`lineage.core.reconcile.MAINTENANCE_OPERATIONS` lists — so the sweep neither reports the version as a
provenance hole nor back-fills it with a run that never existed. Verified against pylance 11.0.0:
`read_version_operations` answers `'UpdateConfig'` for a config commit.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import lance
from pydantic import BaseModel, Field

from maintenance.core.config import shared_lance_session
from maintenance.services.orphans import OrphanFile


if TYPE_CHECKING:
    from maintenance.core.config import MaintenanceSettings

log = logging.getLogger(__name__)

#: The config key the commit writes. Namespaced, and carrying the epoch it was raised at, so an
#: operator reading a dataset's config can tell this commit apart from a real one without the register.
FLOOR_KEY = "rask.maintenance.floor_raised_at"


class FloorRaise(BaseModel):
    """One dataset's outcome. Named rather than counted: this writes to a governed table, so which
    tables were written to is the whole audit trail, and a count answers nobody's question about it."""

    dataset: str
    #: Orphan files this raise is expected to make visible to the next sweep.
    orphan_files: int
    orphan_bytes: int
    #: The floor before the commit, as an epoch — what the orphans were measured against.
    floor_before: float
    #: The version the commit created. ``None`` on a dry run or a refusal.
    version: int | None = None
    #: Why nothing was written. ``None`` when the commit happened (or would have, on a dry run).
    refused: str | None = None


class FloorReport(BaseModel):
    """What one tick did. ``dry_run`` distinguishes a plan from an act, exactly as the trash purge does."""

    enabled: bool = False
    dry_run: bool = True
    raised: list[FloorRaise] = Field(default_factory=list)
    refused: list[FloorRaise] = Field(default_factory=list)
    #: Datasets eligible beyond `max_per_tick`. Named because a truncated pass that logged nothing would
    #: look like a complete one, and the next tick's smaller number would read as progress nobody made.
    capped: int = 0


def _eligible(orphans: list[OrphanFile]) -> dict[str, list[OrphanFile]]:
    """Group orphans by dataset, keeping only datasets where EVERY orphan is stranded above the floor.

    THREE CLASSES, and only one of them is this module's business. ``reclaimable_by_lance is True`` is a
    file waiting out the 7-day rule — time clears it and a commit written for it is a phantom version
    bought for nothing. ``None`` means the floor could not be read, and "we could not tell" must never
    become "act": the whole justification for writing to a governed table is that the residue is
    provably permanent, and an unread floor proves nothing. Only ``False`` is stranded.

    ALL-OR-NOTHING PER DATASET rather than per file, because the commit is per dataset: raising the
    floor for one stranded file also sweeps up its siblings whatever their class, so a dataset holding
    even one undecided orphan is one this pass has not established a case for.
    """
    by_dataset: dict[str, list[OrphanFile]] = {}
    for orphan in orphans:
        by_dataset.setdefault(orphan.dataset, []).append(orphan)
    return {uri: found for uri, found in by_dataset.items() if all(o.reclaimable_by_lance is False for o in found)}


def _current_floor(uri: str, storage_options: dict[str, str]) -> float | None:
    """This dataset's earliest retained manifest timestamp, as an epoch, or ``None`` if unreadable.

    ``.timestamp()`` rather than comparing datetimes, for the reason `orphans._classify_against_floor`
    measured: ``versions()[i]["timestamp"]`` is NAIVE LOCAL while `FileInfo.mtime` is TZ-AWARE UTC, so
    the two are the same instant written hours apart and comparing them raises on the mix.
    """
    try:
        dataset = lance.dataset(uri, storage_options=storage_options, session=shared_lance_session())
        return min(version["timestamp"].timestamp() for version in dataset.versions())
    except Exception as exc:  # noqa: BLE001 — an unreadable dataset refuses this raise, never the tick
        log.warning("floor_read_failed", extra={"dataset": uri, "error": str(exc)})
        return None


def raise_listing_floors(settings: MaintenanceSettings, *, orphans: list[OrphanFile], storage_options: dict[str, str]) -> FloorReport:
    """Write one metadata commit per stranded dataset, so the next sweep can see its residue.

    OFF BY DEFAULT and DRY-RUN BY DEFAULT — two separate switches, because they answer different
    questions. `MAINTENANCE_FLOOR_RAISE_ENABLED` is whether this estate wants the behaviour at all;
    `MAINTENANCE_FLOOR_RAISE_DRY_RUN` is whether this tick acts. An operator turning the feature on to
    SEE what it would touch must not thereby write to a governed table.

    THE FLOOR IS RE-READ HERE, inside the same call, and a dataset whose floor has moved past its
    orphans is refused. The report this consumes was produced earlier in the tick, and the cheap failure
    is acting on a stale one: a dataset that gained a commit in between no longer needs this, and the
    only thing a raise would buy it is the phantom version. One `versions()` read per candidate, no
    re-listing — the report already carries each orphan's mtime, which is what makes that sufficient.
    """
    report = FloorReport(enabled=settings.floor_raise_enabled, dry_run=settings.floor_raise_dry_run)
    if not settings.floor_raise_enabled:
        return report
    candidates = _eligible(orphans)
    for uri in sorted(candidates)[: settings.floor_raise_max_per_tick]:
        found = candidates[uri]
        outcome = FloorRaise(
            dataset=uri,
            orphan_files=len(found),
            orphan_bytes=sum(o.size_bytes for o in found),
            floor_before=min((o.mtime_epoch for o in found), default=0.0),
        )
        floor = _current_floor(uri, storage_options)
        if floor is None:
            report.refused.append(outcome.model_copy(update={"refused": "floor unreadable"}))
            continue
        outcome.floor_before = floor
        if all(o.mtime_epoch <= floor for o in found):
            # The floor already covers them: something committed to this dataset between the report and
            # now, so the next sweep will reclaim without help and a commit here buys only a version.
            report.refused.append(outcome.model_copy(update={"refused": "floor already covers these orphans"}))
            continue
        if report.dry_run:
            report.raised.append(outcome)
            continue
        try:
            # THE SHARED SESSION, like every other dataset open in this service. This pod runs against
            # a 512Mi limit it has already been OOMKilled against, and a store rebuild strands datasets
            # in bulk — a per-dataset session here is the shape that turns a recovery into an eviction.
            lance.dataset(uri, storage_options=storage_options, session=shared_lance_session()).update_config({FLOOR_KEY: str(int(time.time()))})
            outcome.version = lance.dataset(uri, storage_options=storage_options, session=shared_lance_session()).version
        except Exception as exc:  # noqa: BLE001 — one dataset's failure must not stop the others
            report.refused.append(outcome.model_copy(update={"refused": f"commit failed: {exc}"}))
            continue
        report.raised.append(outcome)
    report.capped = max(0, len(candidates) - settings.floor_raise_max_per_tick)
    return report
