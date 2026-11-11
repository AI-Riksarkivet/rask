"""Raising a dataset's LISTING FLOOR so Lance can reclaim residue it has gone blind to ([[LH-094]]).

**This module deletes nothing, and that is the whole design.** It writes ONE metadata-only commit to a
dataset whose orphans sit above its listing floor, which moves the floor above them; the DELETE is then
`cleanup_old_versions`'s on the table's next pass, under Lance's own rule. So the orphan half of
reclamation stays what `purge.py` says it is — report-only, never a path this service names and removes.

WHY A FLOOR EXISTS AT ALL. `cleanup_old_versions` clamps its listing of `_transactions/` and
`_deletions/` to the commit timestamp of the EARLIEST RETAINED MANIFEST BEFORE the 7-day unverified
rule filters what was listed (lance v13.0.0 `rust/lance/src/dataset/cleanup.rs:756-778`). `older_than`
never enters that cutoff. Measured against pylance 11.0.0 with `delete_unverified=True` bypassing the
age filter, the flip is at the manifest timestamp to the second: -3600/-60/-1/0 s REMOVED,
+1/+60/+3600 s KEPT. `data/` and `_indices/` are listed past the floor (lance #8944 for `data/`), so an
orphan there is Lance's whatever its mtime and never needs this pass; `orphans.py` classifies by kind.

SO A TRANSACTION OR DELETION FILE ABOVE THE FLOOR IS PERMANENT, not late. Re-measured 2026-11-10 on pylance 13.0.0
(one unreferenced file planted in each directory an hour either side of the floor, one cleanup with
`delete_unverified=True`): the `data/` and `_indices/` files above the floor were removed, the `_transactions/` and
`_deletions/` ones kept, and every one below it removed. A dataset collapsed to one
live version holds every such file written after that surviving commit forever, and nothing Lance
offers will enumerate it. That is not a pathological case: **any** restore, re-upload or lifecycle
rehydrate rewrites object mtimes above the manifest timestamps that reference them and recreates it
wholesale. Measured on the deployed estate 2026-09-19 — the object store's PVCs were created
`2026-09-11T10:50:09Z` while `m2proof_silver$m2-proof-1788537252` embeds epoch `2026-09-04T15:54:12Z`,
so every object was re-uploaded a week after the commit that references it and the whole dataset sat
above its own floor.

THE COMMIT IS A REAL VERSION AND ITS COST IS ACCEPTED, not hidden (owner ruling 2026-09-19,
`docs/adr/0066-four-rulings-on-the-lakehouse-s-unblocked-rows-owner-2026.md`): the dataset gains a version representing no data change, written for a GC side
effect. The alternative was a one-off deleter over exactly what the scan names, which would have rask
deleting bytes on a live estate against a floor rule it derived itself — and both datasets in the
measured case are LIVE, so their residue is superseded-version material inside governed tables rather
than junk. The asymmetry `base_refs` already states decided it: a wrong refusal costs disk, a wrong
permit costs data.

**A COMMIT ALONE DOES NOT MOVE THE FLOOR — the old manifests must then be CLEANED.** The floor is
the EARLIEST RETAINED manifest, so a new version raises nothing until `cleanup_old_versions` drops the
ones beneath it; the commit is what makes them droppable. That makes this raise USELESS on a dataset
maintenance cannot maintain, and worse than useless, because the orphans stay stranded and every check writes
another version. Measured on the deployed estate 2026-09-19: of the two stranded datasets, maintenance
reclaimed `c6t115034-wh/medallion/bronze` (19 files gone, 32 -> 13) and REFUSED
`m2proof_silver$m2-proof-1788537252` for want of a write credential, whose raise therefore ran twice.
So a table carrying :data:`FLOOR_KEY` is raised again only once its floor has moved past the stamp: then the cleanup
has run beneath the last raise, and a file stranded since earns a raise of its own (measured 2026-11-10 on pylance
13.0.0: a second raise over a moved floor, then one cleanup, removed the transaction file stranded after the first).

THE COMMIT IS SIGNED BY A VENDED, TABLE-SCOPED CREDENTIAL, and a catalog denial REFUSES the raise.
A config commit on a table the catalog refused, signed by whatever opened the dataset, is signed by the
deployment's ambient key: the bypass `credentials.MaintenanceDenied` exists to stop, and version expiry
refuses for the same reason. `write_options_for` returns the scoped credential the door vends and nothing else
([[LH-219]]): it raises `MaintenanceDenied` on a 401/403 and, as its `VendingNotDirect`, where the deployment vends no
direct credential, `TableNotGoverned` on a 404 naming no table and `VendUndecided` when the door cannot be asked or
decides nothing, and each refuses the raise. It is asked only about the table the catalog's ``__manifest`` names the
dataset as: a dataset no row names is refused unvended and unwritten ([[LH-520]]).

THE LINEAGE PLANE ALREADY ANTICIPATES THIS SHAPE. Lance records the operation as `UpdateConfig`, which
`lineage.core.reconcile.MAINTENANCE_OPERATIONS` lists — so lineage's reconcile neither reports the version as a
provenance hole nor back-fills it with a run that never existed. Verified against pylance 11.0.0:
`read_version_operations` answers `'UpdateConfig'` for a config commit.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import lance
from pydantic import BaseModel

from lakehouse.lancekit.versions import committed_at
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import shared_lance_session
from maintenance.services.credentials import MaintenanceDenied, TableNotGoverned, VendUndecided, write_options_for
from maintenance.services.orphans import OrphanFile, listed_at_or_below


if TYPE_CHECKING:
    from maintenance.core.config import MaintenanceSettings

log = logging.getLogger(__name__)


#: The config key the commit writes. Namespaced, and carrying the epoch it was raised at, so an
#: operator reading a dataset's config can tell this commit apart from a real one without the register. To the
#: fraction, as the floor is: a stamp cut to its second could read as passed by a manifest written earlier in it.
FLOOR_KEY = "rask.maintenance.floor_raised_at"


class FloorRaise(BaseModel):
    """One table's raise, or why it was refused. Named rather than counted: this writes to a governed table, so which
    table was written to is the whole audit trail."""

    dataset: str
    #: Stranded files this raise is expected to make visible to the table's next pass.
    orphan_files: int
    orphan_bytes: int
    #: The floor before the commit, as an epoch: what the orphans were measured against.
    floor_before: float
    dry_run: bool
    #: The version the commit created. ``None`` on a dry run or a refusal.
    version: int | None = None
    #: Why nothing was written. ``None`` when the commit happened (or would have, on a dry run).
    refused: str | None = None


class _FloorReading(BaseModel):
    """A dataset's listing floor and the stamp of its last raise, read off one handle."""

    #: The earliest retained manifest's timestamp, as an epoch: compared with `FileInfo.mtime` epochs, and read from the
    #: naive-local manifest time in the right zone by `committed_at` (see `orphans._classify_against_floor`).
    floor: float
    #: The epoch of the last raise as the dataset's own config records it, ``None`` when there has not been one. Kept in
    #: the dataset rather than in any state this service holds, so it survives a pod roll, a replica change and an empty
    #: cache, and an operator can answer the same question by reading the dataset.
    stamp: str | None


def _floor_of(dataset: lance.LanceDataset) -> float:
    """``dataset``'s listing floor: the commit time of its earliest retained version, in epoch seconds."""
    return min(committed_at(version).timestamp() for version in dataset.versions())


def _floor_and_stamp_of(dataset: lance.LanceDataset) -> _FloorReading:
    """``dataset``'s listing floor and the stamp of its last raise, both off this handle, so the two are one reading."""
    return _FloorReading(floor=_floor_of(dataset), stamp=dataset.config().get(FLOOR_KEY))


def _read_floor(uri: str, storage_options: StorageOptions) -> _FloorReading | None:
    """The dataset's floor and stamp from ONE open, or ``None`` when it cannot be read.

    One open, because the stamp is judged against the floor: a second open that failed after the first answered would
    read as "never raised" and commit another raise on a table raised already, a version bought for nothing each time.
    """
    try:
        return _floor_and_stamp_of(lance.dataset(uri, storage_options=storage_options.in_clear(), session=shared_lance_session()))
    except Exception as exc:  # noqa: BLE001 — an unreadable dataset refuses this raise, never the check
        log.warning("floor_read_failed", extra={"dataset": uri, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return None


def _still_beneath(stamp: str, floor: float) -> str | None:
    """Why a raise at ``stamp`` still stands above ``floor``, so another would buy only a version; ``None`` once the floor
    has moved past it, when a pass has cleaned beneath the last raise."""
    try:
        raised_at = float(stamp)
    except ValueError:
        return f"its {FLOOR_KEY} reads {stamp!r}, no epoch, so whether the floor has moved past it cannot be told"
    if floor < raised_at:
        return f"already raised at {stamp} and the floor ({floor:.3f}) has not moved past it: no pass has cleaned beneath the raise yet"
    return None


def raise_stands(dataset: lance.LanceDataset) -> bool:
    """Whether ``dataset`` carries a raise no pass has cleaned beneath yet: its stamp lies above its listing floor.

    A table's pass asks it, and while it holds cleans from the first reclaimable version: the raise bought a version so
    that a cleanup would move the floor, and a pass waiting for its usual count of reclaimable versions would leave the
    stranded files, on a table that commits seldom, for as long as it does. The stamp is read first, so a table never
    raised costs no listing of its versions.
    """
    stamp = dataset.config().get(FLOOR_KEY)
    if stamp is None:
        return False
    try:
        raised_at = float(stamp)
    except ValueError:
        return False
    return _floor_of(dataset) < raised_at


def raise_listing_floor(uri: str, *, table: str, stranded: list[OrphanFile], settings: MaintenanceSettings) -> FloorRaise:
    """Write one metadata-only commit to the table at ``uri`` (the table ``__manifest`` names ``table``), whose ``stranded``
    files lie above its listing floor, so its next pass can reclaim them; or why not.

    Its table's own orphan check calls it, on that check's findings alone, after the guards the table's pass applies
    (the claim records the task's uuid, no trash record keeps the table, the bucket is governed, a ``__manifest`` row
    names the table).

    OFF BY DEFAULT and DRY-RUN BY DEFAULT — two separate switches, because they answer different
    questions. `MAINTENANCE_FLOOR_RAISE_ENABLED` is whether this estate wants the behaviour at all;
    `MAINTENANCE_FLOOR_RAISE_DRY_RUN` is whether this check acts. An operator turning the feature on to
    SEE what it would touch must not thereby write to a governed table.

    THE FLOOR IS RE-READ HERE, with the last raise's stamp off the same handle, and a reading that fails refuses the raise:
    no read answers "never raised" for "could not read". A table whose floor has moved past its stranded files is refused: something
    committed since the check listed them, so the table's next pass reclaims without help and a commit here buys only
    the phantom version. A table carrying :data:`FLOOR_KEY` is refused while its floor stands beneath that stamp, which
    bounds the damage of a raise that cannot work: the floor only moves once a pass cleans the manifests beneath the
    raise, so on a table maintenance may not clean this would write a version per check and reclaim nothing.
    """
    outcome = FloorRaise(
        dataset=uri,
        orphan_files=len(stranded),
        orphan_bytes=sum(orphan.size_bytes for orphan in stranded),
        floor_before=min((orphan.mtime_epoch for orphan in stranded), default=0.0),
        dry_run=settings.floor_raise_dry_run,
    )
    if not settings.floor_raise_enabled:
        return outcome.model_copy(update={"refused": "the floor raise is off (MAINTENANCE_FLOOR_RAISE_ENABLED)"})
    reading = _read_floor(uri, settings.storage_options())
    if reading is None:
        return outcome.model_copy(update={"refused": "the floor and the last raise's stamp could not be read"})
    outcome.floor_before = reading.floor
    if reading.stamp is not None and (beneath := _still_beneath(reading.stamp, reading.floor)) is not None:
        return outcome.model_copy(update={"refused": beneath})
    if all(listed_at_or_below(orphan.mtime_epoch, reading.floor) for orphan in stranded):
        return outcome.model_copy(update={"refused": "floor already covers these orphans"})
    # THE CREDENTIAL IS RESOLVED BEFORE THE DRY-RUN BRANCH, so a preview reports the denial it
    # would hit rather than a plan it could never carry out. A dry run that lists a table the
    # catalog will refuse is the preview telling an operator the opposite of what happens.
    try:
        write_options = write_options_for(uri, settings, table_id=table)
    except (MaintenanceDenied, TableNotGoverned, VendUndecided) as exc:
        return outcome.model_copy(update={"refused": str(exc)})
    if settings.floor_raise_dry_run:
        return outcome
    try:
        # THE SHARED SESSION, like every other dataset open in this service. This pod runs against
        # a 512Mi limit it has already been OOMKilled against, and a store rebuild strands datasets
        # in bulk — a per-dataset session here is the shape that turns a recovery into an eviction.
        written = lance.dataset(uri, storage_options=write_options.in_clear(), session=shared_lance_session())
        written.update_config({FLOOR_KEY: str(time.time())})
        # The handle the commit moved: measured on pylance 13.0.0, `update_config` leaves it at the version it made.
        outcome.version = written.version
    except Exception as exc:  # noqa: BLE001 — a failed commit refuses this raise, and the check holds its next at the floor
        return outcome.model_copy(update={"refused": f"commit failed: {exc}"})
    return outcome
