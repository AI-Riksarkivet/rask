"""Releasing a drop at its deadline (ADR 0146): the trashed object's grants are revoked, its bytes wait for the purge.

A recoverable drop is a DeregisterTable whose bytes stay for re-registration until the trash record's ``expires_at``.
Past it the drop is DropTable's, its data deleted asynchronously (``lance_docs/ns_catalog/spec.yaml`` DropTableRequest),
so nobody may restore it and nobody keeps a grant on it. This step revokes every tuple on the object its record names,
announces a ``grant_revoked`` per principal it named, and stamps the record released (``trash.mark_released``). The
catalog's undrop door refuses the record from the deadline on.

It is apart from the purge (:mod:`maintenance.services.purge`) because it deletes no byte, and so needs neither of the
purge's opt-ins: it runs on every reconcile tick, whatever the drift report says and whether or not
``MAINTENANCE_TRASH_PURGE_ENABLED`` is on, which the default chart ships off. The purge then finds nothing left to
revoke.

Two refusals stand in front of a revoke, each because it would strip a live object's grants:

* **STILL REGISTERED.** An undrop registers and then clears the record, so a crash between the two leaves a record on a
  live table (``purge.still_registered``, the purge's own rule). The manifests are read across every root the registry
  names, deactivated warehouses' included: reading a manifest touches no tenant's bytes, and a root left unread would
  read its live tables as gone.
* **OUTSIDE EVERY ROOT READ.** A table whose location sits under no root this step read cannot be told live or gone.

A record is taken :data:`SETTLE` after its deadline, not at it: an undrop the door admitted just before the deadline
registers before it clears the record, and a revoke landing between the two would leave the restored table ownerless.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from maintenance.core.config import MaintenanceSettings
from maintenance.services.purge import ACTOR, Liveness, liveness_across, still_registered
from service_kit.control_emit import ControlEmitter, NoopControlEmitter, emit_control
from service_kit.governed import fga
from service_kit.lakehouse import trash, warehouse_records
from service_kit.lakehouse.base_refs import location_within
from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)

#: How long past its deadline a record waits before its grants go: well over any one undrop request, which is the
#: window an undrop admitted at the deadline needs to finish. The location claim's lease is the same order.
SETTLE = timedelta(minutes=15)

# Worded for grants, unlike the purge's refusals of the same states, which speak of the bytes they keep.
_FGA_UNWIRED = "FGA is enabled but no client is wired into this process, so no grant can be released"
_STILL_REGISTERED = "still registered: recovered or re-registered since the drop, so its grants are a live object's"
_MANIFEST_UNREADABLE = "a manifest could not be read, so whether the object stands again cannot be told"


class ReleasedRecord(BaseModel):
    """One drop past its deadline whose grants this tick revoked."""

    kind: str
    id: str
    uuid: str
    expires_at: str
    tuples_revoked: int = 0
    #: Whether the record was stamped released. False when it was gone by then (the purge reclaimed it meanwhile) or
    #: the stamp failed: the grants are revoked either way, and a record left unstamped finds none to revoke next tick.
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


class _Tick(BaseModel):
    """What every record of one tick is judged and released against."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    fga_enabled: bool
    storage_options: StorageOptions
    control_root: str
    roots: set[str]
    live: Liveness | None
    fga_client: Any
    control: Any
    now: datetime


def roots_read(settings: MaintenanceSettings, storage_options: StorageOptions, control_root: str) -> set[str]:
    """Every root whose manifest tells whether a trashed object stands again: the swept buckets and every warehouse's.

    Raises if the registry cannot be read: a root left out would read every live table under it as gone.
    """
    records = warehouse_records.list_warehouse_records(control_root, storage_options)
    buckets = {*settings.sweep_buckets, *(str(record["bucket"]) for record in records if record.get("bucket"))}
    return {f"s3://{bucket}" for bucket in buckets if bucket}


def _why_held(record: dict[str, Any], *, roots: set[str], live: Liveness | None) -> str | None:
    if trash.object_uuid(record) is None:
        return "the record keeps no uuid, so the object whose grants it names cannot be told from another"
    if live is None:
        return _MANIFEST_UNREADABLE
    if still_registered(record, live):
        return _STILL_REGISTERED
    location = str(record.get("location") or "").rstrip("/")
    if str(record.get("kind") or "table") == "table" and location and not any(location_within(root, location) for root in roots):
        return f"location {location!r} is under no root this tick read, so whether its table stands again cannot be told"
    return None


async def _release_one(record: dict[str, Any], tick: _Tick) -> ReleasedRecord | HeldRecord:
    """Revoke, announce, then mark one record released. Never raises; a failure keeps the record for the next tick."""
    kind = str(record.get("kind") or "table")
    obj_id = str(record.get("id") or "")
    if (reason := _why_held(record, roots=tick.roots, live=tick.live)) is not None:
        # INFO: the route names every held record at WARNING once per tick.
        log.info("trash_release_held", extra={"kind": kind, "id": obj_id, "reason": reason})
        return HeldRecord(kind=kind, id=obj_id, uuid=trash.object_uuid(record), reason=reason)
    uuid = str(trash.object_uuid(record))
    obj = fga.object_of(kind, uuid)
    try:
        removed = await fga.revoke_object_tuples(tick.fga_client, obj, actor=ACTOR, origin="trash_expired") if tick.fga_enabled else []  # noqa: TID251
    except Exception as exc:  # noqa: BLE001 — a revoke that failed keeps the record unmarked, for the next tick to finish
        log.error("trash_release_revoke_failed", extra={"kind": kind, "id": obj_id, "error": str(exc)})
        return HeldRecord(kind=kind, id=obj_id, uuid=uuid, reason=f"the FGA revoke failed ({type(exc).__name__}: {exc}); the next tick retries")
    # Each person who lost the object is told: none of them can see it any more, so no visibility-gated feed would.
    for tup in removed:
        if fga.names_someone(str(tup.user)):
            await emit_control(
                tick.control,
                action="grant_revoked",
                object_type="grant",
                object_id=obj,
                object_name=obj_id,
                actor=ACTOR,
                extra={"relation": tup.relation, "subject": tup.user, "reason": "trash_expired"},
            )
    try:
        marked = await run_in_threadpool(lambda: trash.mark_released(tick.control_root, tick.storage_options, uuid, kind=kind, now=tick.now))
    except Exception as exc:  # noqa: BLE001 — the grants ARE gone; the next tick finds none to revoke and marks it
        log.error("trash_release_not_marked", extra={"kind": kind, "id": obj_id, "error": str(exc)})
        marked = False
    log.warning("trash_released", extra={"kind": kind, "id": obj_id, "uuid": uuid, "tuples_revoked": len(removed), "marked": marked})
    return ReleasedRecord(kind=kind, id=obj_id, uuid=uuid, expires_at=str(record.get("expires_at") or ""), tuples_revoked=len(removed), marked=marked)


async def release_expired_trash(
    settings: MaintenanceSettings,
    *,
    fga_client: Any = None,  # noqa: ANN401 — OpenFgaClient | None
    control: ControlEmitter | None = None,
    control_root: str | None = None,
    now: datetime | None = None,
) -> TrashReleaseReport:
    """Release every drop :data:`SETTLE` past its deadline whose grants this tick may revoke. Never raises."""
    at = now or datetime.now(UTC)
    out = TrashReleaseReport()
    if settings.fga_enabled and fga_client is None:
        out.reason = _FGA_UNWIRED
        log.error("trash_release_blocked", extra={"reason": out.reason})
        return out
    storage_options = settings.storage_options()
    resolved_control_root = control_root or settings.resolved_control_root
    try:
        records = await run_in_threadpool(trash.list_all, resolved_control_root, storage_options)
        roots = await run_in_threadpool(roots_read, settings, storage_options, resolved_control_root)
    except Exception as exc:  # noqa: BLE001 — an unreadable registry stops the step, it does not fail the tick
        out.reason = f"the trash or the warehouse registry could not be read ({type(exc).__name__}: {exc})"
        log.error("trash_release_blocked", extra={"reason": out.reason})
        return out
    out.ran = True
    due = [record for record in trash.expired(records, now=at - SETTLE) if not trash.is_released(record)]
    if not due:
        return out
    tick = _Tick(
        fga_enabled=settings.fga_enabled,
        storage_options=storage_options,
        control_root=resolved_control_root,
        roots=roots,
        live=await run_in_threadpool(liveness_across, roots, storage_options),
        fga_client=fga_client,
        control=control or NoopControlEmitter(),
        now=at,
    )
    for record in due:
        match await _release_one(record, tick):
            case ReleasedRecord() as released:
                out.released.append(released)
            case HeldRecord() as held:
                out.held.append(held)
    return out
