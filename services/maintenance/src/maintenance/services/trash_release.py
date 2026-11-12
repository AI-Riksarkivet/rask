"""Releasing a drop at its deadline (ADR 0146): the trashed object's grants are revoked, its bytes wait for the purge.

A recoverable drop is a DeregisterTable whose bytes stay for re-registration until the trash record's ``expires_at``.
Past it the drop is DropTable's, its data deleted asynchronously (``lance_docs/ns_catalog/spec.yaml`` DropTableRequest),
so nobody may restore it and nobody keeps a grant on it. The release is the catalog's act, asked of its door for each
record (``POST /management/v1/trash/{kind}/{uuid}/release``, [[XC-084]]): maintenance holds no client that writes grants,
so the catalog revokes every tuple on the object, tells each person who lost one under its own signature, and stamps the
record released. The catalog's undrop door refuses the record from the deadline on.

It is apart from the purge (:mod:`maintenance.services.purge`) because it deletes no byte, and so needs neither of the
purge's opt-ins: it runs on every reconcile tick, whatever the drift report says and whether or not
``MAINTENANCE_TRASH_PURGE_ENABLED`` is on, which the default chart ships off. The purge's ending then finds nothing
left to revoke.

Two refusals stand in front of asking, each because the release would strip a live object's grants, and the catalog
judges both again on its own reads:

* **STILL REGISTERED.** An undrop registers and then clears the record, so a crash between the two leaves a record on a
  live table (``purge.still_registered``, the purge's own rule). A trash task's release reads the ``__manifest`` of the
  one bucket a table record's location is in (:func:`release_record`); the audit's and the scan's backstop
  (:func:`release_expired_trash`), and a task's release of a namespace record, read every root the registry names,
  deactivated warehouses' included: reading a manifest touches no tenant's bytes, and a root left unread would read its
  live tables as gone.
* **OUTSIDE EVERY ROOT READ.** On the backstop's estate-wide read, a table whose location sits under no root it read
  cannot be told live or gone. A task's release reads the root its record's location is under, so it never meets one.

A record is taken :data:`lakehouse.trash.RELEASE_SETTLE` after its deadline, not at it: an undrop the door admitted
just before the deadline registers before it clears the record, and a revoke landing between the two would leave the
restored table ownerless. A refusal the catalog judges it notes on the record; this step reports it as held.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import partial
from typing import Any

from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from lakehouse import trash, warehouse_records
from lakehouse.base_refs import location_within
from lakehouse.storage_options import StorageOptions, on_s3
from maintenance.core.config import MaintenanceSettings
from maintenance.services.credentials import MaintenanceDenied, TableNotGoverned, VendUndecided, release_trash
from maintenance.services.purge import Liveness, liveness_across, still_registered


log = logging.getLogger(__name__)

# Worded for grants, unlike the purge's refusals of the same states, which speak of the bytes they keep.
_STILL_REGISTERED = "still registered: recovered or re-registered since the drop, so its grants are a live object's"
_MANIFEST_UNREADABLE = "a manifest could not be read, so whether the object stands again cannot be told"


class ReleasedRecord(BaseModel):
    """One drop past its deadline whose grants the catalog revoked this tick."""

    kind: str
    id: str
    uuid: str
    expires_at: str
    tuples_revoked: int = 0
    #: Whether the record was stamped released. False when it was gone by then (the purge reclaimed it meanwhile): the
    #: grants are revoked either way, and a record left unstamped finds none to revoke next tick.
    marked: bool = True


class HeldRecord(BaseModel):
    """One drop past its deadline whose grants this tick kept, and why."""

    kind: str
    id: str
    uuid: str | None = None
    reason: str


class TrashReleaseReport(BaseModel):
    """One tick's releases. ``ran`` is false when the step stopped before judging any record, and ``reason`` says why."""

    ran: bool = False
    reason: str | None = None
    released: list[ReleasedRecord] = Field(default_factory=list)
    held: list[HeldRecord] = Field(default_factory=list)


def roots_read(settings: MaintenanceSettings, storage_options: StorageOptions, control_root: str) -> set[str]:
    """Every root whose manifest tells whether a trashed object stands again: the swept buckets and every warehouse's.

    Raises if the registry, or any one record in it, cannot be read (:func:`lakehouse.warehouse_records
    .read_warehouse_records`): a root left out would read every live table under it as gone.
    """
    records = warehouse_records.read_warehouse_records(control_root, storage_options)
    buckets = {*settings.sweep_buckets, *(str(record["bucket"]) for record in records if record.get("bucket"))}
    return {f"s3://{bucket}" for bucket in buckets if bucket}


def _why_held(record: dict[str, Any], *, roots: set[str], live: Liveness | None) -> str | None:
    if trash.object_uuid(record) is None:
        return "the record keeps no uuid, so the object whose grants it names cannot be told from another"
    if live is None:
        return _MANIFEST_UNREADABLE
    if still_registered(record, live):
        return _STILL_REGISTERED
    location = trash.location_of(record)
    if trash.kind_of(record) == "table" and location and not any(location_within(root, location) for root in roots):
        return f"location {location!r} is under no root this tick read, so whether its table stands again cannot be told"
    return None


async def _release_or_hold(record: dict[str, Any], *, roots: set[str], live: Liveness | None, settings: MaintenanceSettings) -> ReleasedRecord | HeldRecord:
    """Release ONE record, and hold it naming the cause of any failure it raises: the step holds "never raises" by
    construction, each record's failure reported beside the others' releases, so a record whose release fails leaves
    the records after it released ([[LH-572]])."""
    try:
        return await _release_one(record, roots=roots, live=live, settings=settings)
    except Exception as exc:  # noqa: BLE001 — one record's failure, whatever it is, holds that record
        log.exception("trash_release_failed", extra={"kind": trash.kind_of(record), "id": trash.name_of(record)})
        reason = f"the release failed ({type(exc).__name__}: {exc}); the record is held and the next tick asks again"
        return HeldRecord(kind=trash.kind_of(record), id=trash.name_of(record), uuid=trash.object_uuid(record), reason=reason)


async def _release_one(record: dict[str, Any], *, roots: set[str], live: Liveness | None, settings: MaintenanceSettings) -> ReleasedRecord | HeldRecord:
    """Ask the catalog to release one record; a record the catalog refuses, or could not decide, is held. A failure it does
    not expect raises, and :func:`_release_or_hold` holds the record."""
    kind, obj_id = trash.kind_of(record), trash.name_of(record)
    if (reason := _why_held(record, roots=roots, live=live)) is not None:
        # INFO: the route names every held record at WARNING once per tick.
        log.info("trash_release_held", extra={"kind": kind, "id": obj_id, "reason": reason})
        return HeldRecord(kind=kind, id=obj_id, uuid=trash.object_uuid(record), reason=reason)
    uuid = str(trash.object_uuid(record))
    try:
        released = await run_in_threadpool(partial(release_trash, kind, uuid, settings))
    except (MaintenanceDenied, TableNotGoverned, VendUndecided) as exc:
        log.warning("trash_release_refused_by_the_catalog", extra={"kind": kind, "id": obj_id, "reason": str(exc)})
        return HeldRecord(kind=kind, id=obj_id, uuid=uuid, reason=str(exc))
    log.warning("trash_released", extra={"kind": kind, "id": obj_id, "uuid": uuid, "tuples_revoked": released.tuples_revoked, "marked": released.stamped})
    return ReleasedRecord(
        kind=kind, id=obj_id, uuid=uuid, expires_at=str(record.get("expires_at") or ""), tuples_revoked=released.tuples_revoked, marked=released.stamped
    )


async def release_expired_trash(settings: MaintenanceSettings, *, control_root: str | None = None, now: datetime | None = None) -> TrashReleaseReport:
    """Release every drop :data:`lakehouse.trash.RELEASE_SETTLE` past its deadline that this step may release: the audit's
    and the scan's backstop for a record no task released. Never raises."""
    at = now or datetime.now(UTC)
    out = TrashReleaseReport()
    resolved_control_root = control_root or settings.resolved_control_root
    try:
        # Read here, once, so a credential outage stops the step with its reason as an unreadable registry does.
        storage_options = settings.storage_options()
        records = await run_in_threadpool(trash.list_all, resolved_control_root, storage_options)
        roots = await run_in_threadpool(roots_read, settings, storage_options, resolved_control_root)
        due = [record for record in records if trash.releasable(record, now=at) and not trash.is_released(record)]
        live = await run_in_threadpool(liveness_across, roots, storage_options) if due else None
    except Exception as exc:  # noqa: BLE001 — an unreadable session, registry or estate stops the step, it does not fail the tick
        out.reason = f"the trash, the warehouse registry or the estate could not be read ({type(exc).__name__}: {exc})"
        log.error("trash_release_blocked", extra={"reason": out.reason})
        return out
    out.ran = True
    for record in due:
        _reported(out, await _release_or_hold(record, roots=roots, live=live, settings=settings))
    return out


async def release_record(record: dict[str, Any], settings: MaintenanceSettings, *, now: datetime | None = None) -> TrashReleaseReport:
    """Release the one drop ``record`` keeps, read by its key, once it is :data:`lakehouse.trash.RELEASE_SETTLE` past its
    deadline: a trash task's release at its own deadline. Never raises.

    What it reads is the record's, never the estate's (ADR 0198 §2): whether a table stands again is asked of the
    ``__manifest`` of the bucket its location is in (:func:`root_of`), where every row naming that location is, since a
    table the catalog makes, registers or restores is recorded relative to the root it is laid out under. A record that
    names no such place, a namespace's, has no one root, and is judged against every root the registry names, as the
    audit's backstop judges it. The catalog judges the record again on its own reads before it revokes anything.
    """
    out = TrashReleaseReport()
    if not trash.releasable(record, now=now or datetime.now(UTC)) or trash.is_released(record):
        out.ran = True
        return out
    try:
        storage_options = settings.storage_options()
        own = root_of(record)
        roots = {own} if own is not None else await run_in_threadpool(roots_read, settings, storage_options, settings.resolved_control_root)
        live = await run_in_threadpool(liveness_across, roots, storage_options)
    except Exception as exc:  # noqa: BLE001 — an unreadable session, registry or estate stops the step, it does not fail the task
        out.reason = f"the warehouse registry or the estate could not be read ({type(exc).__name__}: {exc})"
        log.error("trash_release_blocked", extra={"reason": out.reason, "uuid": trash.object_uuid(record)})
        return out
    out.ran = True
    _reported(out, await _release_or_hold(record, roots=roots, live=live, settings=settings))
    return out


def root_of(record: dict[str, Any]) -> str | None:
    """The root whose ``__manifest`` records every table at ``record``'s location: the bucket a table record's ``s3://``
    location is in; ``None`` for a record naming no such place (a namespace's, or a table recorded with no location)."""
    location = trash.location_of(record)
    if trash.kind_of(record) != "table" or not on_s3(location):
        return None
    bucket = location.removeprefix("s3://").split("/", 1)[0]
    return f"s3://{bucket}" if bucket else None


def _reported(out: TrashReleaseReport, judged: ReleasedRecord | HeldRecord) -> None:
    match judged:
        case ReleasedRecord() as released:
            out.released.append(released)
        case HeldRecord() as held:
            out.held.append(held)
