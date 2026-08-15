"""The expired-trash purge (#79) — the ONLY module in this service that reclaims a dropped object.

Everything mutating for reclamation lives here so that :mod:`maintenance.services.reconcile` stays
report-only and its AST gate (``tests/unit/test_reconcile_report.py``) stays green. The split is the
design, not a filing convention: the report is what earns the delete permission, and a reporter that can
delete cannot be trusted to have earned it.

**Trash purge first, orphan reclaim later — deliberately.** A trash record names ONE path that the
catalog itself recorded at drop time, so the delete is bounded and its target is a fact. The orphan half
is prefix subtraction: "list the bucket, subtract what a live manifest references", a rule with five
documented false-positive classes (branches, multi-base clones, MemWAL shards, data overlays, blob
sidecars) whose first live run called 29 MB of real page images reclaimable. That half stays REPORT-ONLY.

What the purge does per record, in this order, each step idempotent so a crash mid-record re-runs the
whole record cleanly on the next tick:

1. **revoke the FGA tuples FIRST.** A revoke that fails REFUSES the record with the bytes untouched.
   Grants must never outlive the bytes they were written for: if the id is later reused, stale tuples
   silently re-grant the old subjects on the new object. The reverse order would mean a delete that
   succeeded followed by a revoke that failed — bytes gone, grants live, and nothing left to retry
   against.
2. **delete the recorded path.** The file listing that sums the reclaimed bytes doubles as the existence
   probe; an already-absent path is idempotent success.
3. **clear the trash record LAST**, so any failure above leaves the record and the next tick retries.
4. **announce it** on the control stream (best-effort).

Four refusals stand in front of all of that, in order, and each exists because of a specific way this
could destroy live data:

* **STILL REGISTERED.** The catalog's undrop registers and THEN clears the record
  (``tables.py`` / ``namespaces.py``); a crash in between — or a manual ``register_table`` at the same
  location — leaves a LIVE table with a stale trash record. Purging that record deletes a live table's
  bytes. So the id is re-checked against ``__manifest`` (the spec's own object index, which is the only
  place a namespace exists at all) immediately before deleting, and a manifest that cannot be READ
  refuses EVERY record rather than degrading to "purge anyway".
* **namespace / empty location.** A ``kind="namespace"`` record (#96) and a declared-only table both
  carry ``location=""`` — there are no bytes, so the path checks do not apply and the record is revoked
  and cleared without a delete.
* **outside the maintained estate.** A record's ``location`` is DATA; nothing constrains it. It must
  resolve under a bucket this service maintains (the configured sweep buckets plus every registered,
  non-deactivated warehouse) or it is refused. A deactivated warehouse is outside that set on purpose:
  quarantine means nobody touches those bytes, least of all a reclaimer.
* **a store root or a control prefix.** ``s3://bkt`` as a location would ``delete_dir`` a whole bucket;
  a path through ``_trash``/``_projects``/``_warehouses``/``_policies``/``_protection``/``__manifest``
  would delete the estate's own registries.

And one gate stands in front of the whole pass: :func:`report_is_clean`. The purge consumes the drift
report from the SAME tick, and reclaims nothing unless that report found no drift, could check every
category it did not deliberately skip, and completed every scan it started.

**Not proven against real object storage yet.** The suite below drives a real local-filesystem control
root and real Lance datasets through the real backend, which is what makes the refusal ladder honest —
but #80 (the sweep vs a live RustFS estate) is still open, so "tests green" is not "reclamation
witnessed". The flag ships OFF.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any

import lance
import pyarrow.fs as pafs
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from maintenance.core.config import MaintenanceSettings, shared_lance_session
from maintenance.core.control_emit import ControlEmitter, NoopControlEmitter, emit_control
from maintenance.core.metrics import record_trash_purge
from maintenance.services.base_refs import BaseRefs
from maintenance.services.reconcile import MANIFEST_DIR, ReconcileReport
from service_kit.control_events import ControlAction, ControlObjectType
from service_kit.governed import fga
from service_kit.lakehouse import trash, warehouse_records
from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)

#: The principal recorded on every revoke and every control event. Not a user: nobody asked for this
#: delete at the moment it happens — the grace period expiring did. An audit row naming the dropper
#: would be a lie about who acted.
ACTOR = "service:maintenance"

#: Prefixes on the control root that are the ESTATE'S OWN STATE, not any object's bytes. A location whose
#: path crosses one of these is refused outright. The check covers EVERY segment, not just the last: a
#: location of ``s3://bkt/_trash/x`` names a file inside the trash registry, and refusing only when the
#: FINAL segment matches would delete it.
CONTROL_PREFIXES = frozenset({MANIFEST_DIR, "_trash", "_projects", "_warehouses", "_policies", "_protection"})

_STILL_REGISTERED = "still registered — recovered or re-registered since the drop, so these bytes are LIVE"
_MANIFEST_UNREADABLE = "the object manifest could not be read, so liveness cannot be re-checked — refusing to delete blind"
_PURGE_OFF = (
    "MAINTENANCE_TRASH_PURGE_ENABLED is off — reclamation is opt-in and report-only is the shipped default (turning it on makes an expired drop unrecoverable)"
)
_FGA_UNWIRED = "FGA is enabled but no client is wired into this process — grants cannot die with the bytes, so nothing is purged"


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


class PurgedRecord(BaseModel):
    """One expired trash record whose bytes and grants are now gone."""

    kind: str
    id: str
    #: The path that was deleted — ``""`` for a namespace record, which owns no bytes.
    location: str
    expires_at: str
    bytes_deleted: int = 0
    files_deleted: int = 0
    tuples_revoked: int = 0


class RefusedRecord(BaseModel):
    """One expired trash record the purge declined to act on, and WHY.

    A refusal is a finding, not an error: the common cases (still registered, outside the maintained
    estate) are precisely the states where deleting would have destroyed something live.
    """

    kind: str
    id: str
    reason: str


class TrashPurgeReport(BaseModel):
    """One tick's reclamation, machine-readable and honest about what it did NOT do.

    ``ran`` distinguishes "the purge executed and found nothing due" from "the purge never got past its
    gates" — the same distinction the drift report keeps between a zero count and an absent category.
    ``due`` is only meaningful when ``ran`` is true or the gate blocked after the listing: with the flag
    off nothing is listed at all, and ``reason`` says so.
    """

    enabled: bool = False
    ran: bool = False
    #: Why the purge did not run, when it did not. ``None`` on a run.
    reason: str | None = None
    due: int = 0
    purged: list[PurgedRecord] = Field(default_factory=list)
    refused: list[RefusedRecord] = Field(default_factory=list)
    #: Records past the per-tick cap. REPORTED, never silently dropped — the next tick takes them.
    capped: int = 0
    #: Drift categories the report deliberately skipped. They do NOT block the purge (the shipped
    #: config skips ``orphan_files``, so blocking on a skip would make reclamation unreachable), but
    #: they are copied here so a purge can never read as "the whole estate was verified".
    skipped_categories: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Selection + the gate
# --------------------------------------------------------------------------- #


def due_records(control_root: str, storage_options: StorageOptions) -> list[dict[str, Any]]:
    """Every trash record past its deadline, OLDEST FIRST.

    One rule for "what is expired", shared with the sweep's report-only log, so the thing that gets
    reported and the thing that gets deleted can never be two different sets. ``trash.expired`` refuses
    an undated or unparseable record, which is the fail-toward-not-deleting direction.

    Deterministic oldest-first ordering, NOT the shuffle the FAIL-emit cap uses: that cap shuffles so
    every failing dataset eventually gets an event, while here the over-cap remainder is drained on the
    next tick anyway and a stable order makes a capped run reproducible.
    """
    return sorted(trash.expired(trash.list_all(control_root, storage_options)), key=lambda r: str(r.get("expires_at") or ""))


def report_is_clean(report: ReconcileReport) -> str | None:
    """The gate: ``None`` when this tick's drift report certifies the estate, else the blocking reason.

    CLEAN := no findings, no category unavailable, no scan incomplete, and no skipped category that
    represents a COVERAGE GAP.

    A skip blocks or not depending on WHICH KIND it is (#128a), and this used to be a blanket pass.
    The old docstring defended that with: "the shipped configuration skips ``orphan_files`` … so
    treating a skip as drift would make the purge unreachable in every real deployment." That was
    true, and the reasoning was CIRCULAR — it held only because `orphan_scan_enabled` defaulted False
    and the chart shipped no lever to turn it on, so the scan could never run anywhere. The purge
    therefore reclaimed bytes against a file layer nobody had ever inspected, and the report printed
    the same thing for "we looked and it was clean" as for "we didn't look".

    So the two kinds are now distinguished by `CategorySkipped.coverage_gap`:

      * NOT a gap — ``unbound_namespaces`` with warehouses off. The rule does not apply; there are no
        bindings, nothing was missed. Blocking would make the purge unreachable on every single-bucket
        deployment over a question with no answer to give.
      * A GAP — ``orphan_files`` with the scan off. The rule applies and we chose not to run it.

    The lever landed with this change (chart ``maintenance.orphanScan``), which is what makes blocking
    reachable rather than fatal: an operator who wants the purge turns the scan on.

    An UNAVAILABLE or INCOMPLETE category blocks for the neighbouring reason: those are questions the
    report tried to answer and could not, and reclaiming on the strength of a scan that half-failed is
    the failure mode the whole report-first rule exists to prevent.

    Order matters. Real findings are reported FIRST because they are immediately actionable, whereas a
    gap is a configuration change — a caller staring at "turn the scan on" while three orphans sit
    unmentioned is being told the less useful of two true things.
    """
    if report.total:
        drifting = sorted(name for name, count in report.counts.items() if count)
        return f"the drift report is NOT clean: {report.total} finding(s) across {drifting}"
    if report.unavailable:
        return f"the drift report could not check {sorted(u.category for u in report.unavailable)} — a category nobody looked at is not a clean one"
    if report.incomplete:
        return f"the drift report is INCOMPLETE ({sorted({i.source for i in report.incomplete})}) — a partial scan cannot certify the estate"
    # EVERY gap, not just the first: an operator who enables one scan and re-runs must not discover a
    # second gap, then a third, each round trip costing a pass that opens every dataset in the estate.
    if gaps := sorted(s.category for s in report.skipped if s.coverage_gap):
        return f"the drift report SKIPPED {gaps} — that part of the estate was never examined, so this report cannot certify it"
    return None


# --------------------------------------------------------------------------- #
# The maintained estate + the liveness re-check
# --------------------------------------------------------------------------- #


def maintained_roots(settings: MaintenanceSettings, storage_options: StorageOptions) -> set[str]:
    """Every root URI whose bytes this service is allowed to reclaim.

    The configured sweep buckets UNION every bucket the warehouse registry names — the same runtime-read
    the sweep does (#81), because a warehouse bucket is created by an API call and a config-time list is
    stale by construction. ``maintainable_buckets`` already drops DEACTIVATED warehouses, and that
    exclusion matters more here than it does for the sweep: deactivate is offboarding step one, and
    destroying a quarantined tenant's dropped tables is not a maintenance decision to make on their
    behalf.

    Raises if the registry cannot be read. The caller turns that into a refusal to run — narrowing the
    estate silently would be safe for the purge, but "we could not enumerate what we maintain" is
    exactly the moment a reclaimer should stop rather than proceed on a partial map.
    """
    buckets = set(settings.sweep_buckets)
    buckets |= set(warehouse_records.maintainable_buckets(warehouse_records.list_warehouse_records(settings.resolved_control_root, storage_options)))
    return {f"s3://{bucket}" for bucket in buckets if bucket}


def live_object_ids(root: str, storage_options: StorageOptions) -> set[str] | None:
    """Every object id ``root``'s ``__manifest`` records — tables AND namespaces — or ``None``.

    ``None`` means UNREADABLE, and the caller must treat it as "refuse everything". A missing manifest is
    NOT unreadable: a root with no ``_versions`` directory is a fresh estate and yields the empty set (the
    same probe-first shape :func:`reconcile._top_level_namespaces` uses, so an absent dataset never rides
    an exception). The difference is load-bearing — collapsing the two would let a broken manifest read
    as "nothing is live" and authorise deleting all of it.

    No ``object_type`` filter, unlike the reconciler's read: a table record and a namespace record are
    both checked against this set, and a namespace is only ever a manifest ROW.
    """
    try:
        fs, base = fs_and_base(root, storage_options)
        if fs.get_file_info(f"{base}/{MANIFEST_DIR}/_versions").type != pafs.FileType.Directory:
            return set()
        manifest_uri = f"s3://{base}/{MANIFEST_DIR}" if root.startswith("s3://") else f"{base}/{MANIFEST_DIR}"
        dataset = lance.dataset(manifest_uri, storage_options=storage_options, session=shared_lance_session())  # ty: ignore[invalid-argument-type] — stub lacks session=, runtime verified
        return {str(v) for v in dataset.to_table(columns=["object_id"]).column("object_id").to_pylist() if v}
    except Exception:  # noqa: BLE001 — ANY failure here must fail toward not-deleting, not toward a partial set
        log.warning("trash_purge_manifest_unreadable", extra={"root": root}, exc_info=True)
        return None


def live_ids_across(roots: set[str], storage_options: StorageOptions) -> set[str] | None:
    """The union of every maintained root's live object ids, or ``None`` if ANY root was unreadable.

    All-or-nothing on purpose. A ``kind="namespace"`` record carries no root of its own, so its liveness
    question is "is this id registered ANYWHERE in the estate" — an answer computed from a subset of the
    roots is not an answer, it is a smaller search that finds fewer live objects and therefore deletes
    more. One unreadable manifest poisons the whole tick; the next tick retries.

    Operational consequence, stated rather than discovered: a warehouse whose bucket the registry names
    but this process cannot REACH blocks reclamation estate-wide until it is fixed or the record is
    removed. That is the intended direction — an unreachable part of the estate is a part nobody checked
    — and it is visible: every failing root is logged by name (``trash_purge_manifest_unreadable``) and
    the refusal reason lands on every record. A bucket that merely has no ``__manifest`` yet is NOT this
    case; it lists as absent and contributes the empty set.
    """
    live: set[str] = set()
    for root in sorted(roots):
        ids = live_object_ids(root, storage_options)
        if ids is None:
            return None
        live |= ids
    return live


def _root_of(location: str, roots: set[str]) -> str | None:
    """The maintained root ``location`` sits under, or ``None``. Longest match wins (nested roots)."""
    for root in sorted((r.rstrip("/") for r in roots), key=len, reverse=True):
        if location == root or location.startswith(f"{root}/"):
            return root
    return None


def check(record: dict[str, Any], *, roots: set[str], live_ids: set[str] | None) -> str | None:
    """The refusal ladder — ``None`` to purge this record, else the reason not to. Order matters.

    Liveness is checked FIRST because it is the only refusal that protects data someone still has: the
    others protect the estate from a malformed record, this one protects a table the catalog just
    recovered. Path checks come after, and are skipped entirely for records that name no bytes.
    """
    obj_id = str(record.get("id") or "")
    if not obj_id:
        return "the record names no object id"
    if live_ids is None:
        return _MANIFEST_UNREADABLE
    if obj_id in live_ids:
        return _STILL_REGISTERED
    kind = str(record.get("kind") or "table")
    location = str(record.get("location") or "").rstrip("/")
    if kind == "namespace" or not location:
        # A namespace is a manifest ROW and a declared-only table never had bytes: nothing to delete, so
        # nothing to path-check. Revoke + clear still apply.
        return None
    root = _root_of(location, roots)
    if root is None:
        return f"location {location!r} is outside the maintained estate {sorted(roots)}"
    remainder = location[len(root) :].strip("/")
    if not remainder:
        return f"location {location!r} IS a store root — refusing to delete a whole bucket"
    if crossed := sorted(CONTROL_PREFIXES.intersection(remainder.split("/"))):
        return f"location {location!r} crosses the control prefix(es) {crossed} — refusing to delete the estate's own registries"
    return None


# --------------------------------------------------------------------------- #
# The mutations
# --------------------------------------------------------------------------- #


class ProtectedBaseError(RuntimeError):
    """Refusal: another dataset's manifest resolves its files through this location (#128d).

    Its own type rather than a bare RuntimeError because the caller must be able to report this as a
    REFUSAL — a deliberate, correct non-deletion — and not as an error that failed the purge.
    """


def delete_location(location: str, storage_options: StorageOptions, *, protected: BaseRefs | None = None) -> tuple[int, int]:
    """Delete the recorded dataset directory; return ``(bytes_deleted, files_deleted)``.

    The recursive listing that sums the bytes doubles as the existence probe — one round trip answers
    both "is anything there" and "how much did we reclaim". ``delete_dir`` on an already-absent path is
    idempotent success (a crash between the delete and the record clear re-runs the whole record); ANY
    other ``OSError`` propagates, because ``pyarrow`` raises ``ArrowIOError`` (an ``OSError``) for a
    RustFS/S3 5xx and swallowing that would report a purge over bytes that are still there.

    ``protected`` is the #128d guard: the estate's foreign ``base_paths`` collected by
    :func:`maintenance.services.base_refs.protected_roots`. When this location is one of them — or
    sits under one — a live shallow clone resolves its data files through these bytes and deleting
    them breaks it. Refuses with :class:`ProtectedBaseError` rather than deleting.

    Passing ``None`` skips the check, and that is for callers that have already run the pre-pass, not
    a default to lean on: the SOURCE of a clone carries no feature flag and no ``base_paths`` of its
    own (measured), so nothing about the dataset in front of you reveals the danger. Only the
    cross-estate pre-pass can.
    """
    if protected is not None and (root := protected.is_protected(location)) is not None:
        raise ProtectedBaseError(
            f"refusing to delete {location}: another dataset resolves its files through {root} "
            f"(shallow clone / multi-base) — deleting these bytes would break a live dataset"
        )
    fs, path = fs_and_base(location, storage_options)
    infos = fs.get_file_info(pafs.FileSelector(path, recursive=True, allow_not_found=True))
    files = [info for info in infos if info.type == pafs.FileType.File]
    with suppress(FileNotFoundError):
        fs.delete_dir(path)
    return sum(int(info.size or 0) for info in files), len(files)


async def _revoke(fga_client: Any, *, kind: str, obj_id: str, fga_enabled: bool) -> int:  # noqa: ANN401 — OpenFgaClient, no protocol
    """Revoke every tuple on the object, returning the count. ``0`` when FGA is off estate-wide.

    ``lifecycle_delete`` is the origin an audit row needs to say this was a retirement rather than an
    admin's choice. With FGA disabled there are no tuples to revoke and skipping is correct — the
    misconfigured case (enabled but unwired) is refused before any record is touched.
    """
    if not fga_enabled:
        return 0
    return await fga.revoke_object_tuples(fga_client, f"{kind}:{obj_id}", actor=ACTOR, origin="lifecycle_delete")


#: The control-plane vocabulary for a purge.
#:
#: DELIBERATE REUSE of the existing drop actions rather than new ``table_purged`` / ``namespace_purged``
#: members. ``ControlAction`` is a wire contract across three files — the Literal, ``docs/catalog-openapi.json``
#: and ``frontend/packages/api/src/generated/catalog.ts`` — and adding a member without regenerating all
#: three leaves the TS client unable to NAME an event the backend publishes. The regen could not be done
#: in this change, so the purge speaks the vocabulary that already exists: the record's `reason` in
#: `extra` is what distinguishes "the grace period ran out" from the original drop, and a consumer that
#: re-reads state on either one is correct in both cases (an event is a refresh hint, never data).
_PURGE_ACTION: dict[str, ControlAction] = {"table": "table_dropped", "namespace": "namespace_dropped"}


async def _purge_one(
    record: dict[str, Any],
    out: TrashPurgeReport,
    *,
    settings: MaintenanceSettings,
    storage_options: StorageOptions,
    control_root: str,
    roots: set[str],
    live_ids: set[str] | None,
    fga_client: Any,  # noqa: ANN401 — OpenFgaClient | None
    control: ControlEmitter,
) -> None:
    """Revoke → delete → clear → announce, for ONE record. Never raises; every failure is a refusal."""
    kind = str(record.get("kind") or "table")
    obj_id = str(record.get("id") or "")
    location = str(record.get("location") or "").rstrip("/")

    if (reason := check(record, roots=roots, live_ids=live_ids)) is not None:
        out.refused.append(RefusedRecord(kind=kind, id=obj_id, reason=reason))
        log.warning("trash_purge_refused", extra={"kind": kind, "id": obj_id, "reason": reason})
        return

    try:
        revoked = await _revoke(fga_client, kind=kind, obj_id=obj_id, fga_enabled=settings.fga_enabled)
    except Exception as exc:  # noqa: BLE001 — a revoke we could not perform is a reason not to delete
        reason = f"the FGA revoke failed ({type(exc).__name__}: {exc}) — grants must never outlive the bytes, so nothing was deleted"
        out.refused.append(RefusedRecord(kind=kind, id=obj_id, reason=reason))
        log.error("trash_purge_revoke_failed", extra={"kind": kind, "id": obj_id, "error": str(exc)})
        return

    deleted_bytes = deleted_files = 0
    if location and kind != "namespace":
        try:
            deleted_bytes, deleted_files = await run_in_threadpool(delete_location, location, storage_options)
        except OSError as exc:
            reason = f"deleting {location!r} failed ({type(exc).__name__}: {exc}) — the record survives and the next tick retries"
            out.refused.append(RefusedRecord(kind=kind, id=obj_id, reason=reason))
            log.error("trash_purge_delete_failed", extra={"kind": kind, "id": obj_id, "location": location, "error": str(exc)})
            return

    try:
        await run_in_threadpool(trash.clear, control_root, storage_options, obj_id, kind=kind)
    except Exception as exc:  # noqa: BLE001 — the bytes ARE gone; say so rather than claiming a clean purge
        reason = f"the bytes were deleted and the grants revoked, but the trash record could not be cleared ({type(exc).__name__}: {exc}) — the next tick retries idempotently"
        out.refused.append(RefusedRecord(kind=kind, id=obj_id, reason=reason))
        log.error("trash_purge_record_not_cleared", extra={"kind": kind, "id": obj_id, "error": str(exc)})
        return

    out.purged.append(
        PurgedRecord(
            kind=kind,
            id=obj_id,
            location=location,
            expires_at=str(record.get("expires_at") or ""),
            bytes_deleted=deleted_bytes,
            files_deleted=deleted_files,
            tuples_revoked=revoked,
        )
    )
    log.warning(
        "trash_purged",
        extra={"kind": kind, "id": obj_id, "location": location, "bytes": deleted_bytes, "files": deleted_files, "tuples_revoked": revoked},
    )
    object_type: ControlObjectType = "namespace" if kind == "namespace" else "table"
    await emit_control(
        control,
        action=_PURGE_ACTION.get(kind, "table_dropped"),
        object_type=object_type,
        object_id=f"{kind}:{obj_id}",
        actor=ACTOR,
        extra={
            "reason": "trash_expired",
            "expires_at": str(record.get("expires_at") or ""),
            "bytes_deleted": deleted_bytes,
            "tuples_revoked": revoked,
        },
    )


async def purge_expired_trash(
    settings: MaintenanceSettings,
    *,
    report: ReconcileReport,
    fga_client: Any = None,  # noqa: ANN401 — OpenFgaClient | None
    control: ControlEmitter | None = None,
    control_root: str | None = None,
    data_roots: set[str] | None = None,
) -> TrashPurgeReport:
    """Reclaim every expired trash record this tick is allowed to reclaim.

    ``report`` MUST be the report THIS tick produced — the gate is "the drift report ran clean", and a
    stale report certifies a state that no longer exists. ``data_roots`` overrides the registry-derived
    maintained estate (tests drive a ``file://`` root; production never passes it).

    Never raises: every failure lands in the returned report, because a reclamation pass that 500s tells
    an operator nothing about which records it got to first.
    """
    control = control or NoopControlEmitter()
    out = TrashPurgeReport(enabled=settings.trash_purge_enabled, skipped_categories=[s.category for s in report.skipped])

    if not settings.trash_purge_enabled:
        out.reason = _PURGE_OFF
        return out
    if (blocker := report_is_clean(report)) is not None:
        out.reason = blocker
        log.warning("trash_purge_blocked", extra={"reason": blocker})
        return out
    # The upstream report already degrades all four authz categories to UNAVAILABLE when FGA is enabled
    # without a client, so this is unreachable through the cron route — kept because `purge_expired_trash`
    # is callable on its own and the consequence (bytes gone, grants live) is not one to leave to layering.
    if settings.fga_enabled and fga_client is None:
        out.reason = _FGA_UNWIRED
        log.error("trash_purge_blocked", extra={"reason": out.reason})
        return out

    storage_options = settings.storage_options()
    resolved_control_root = control_root or settings.resolved_control_root
    try:
        roots = data_roots if data_roots is not None else await run_in_threadpool(maintained_roots, settings, storage_options)
        due = await run_in_threadpool(due_records, resolved_control_root, storage_options)
    except Exception as exc:  # noqa: BLE001 — an unreadable registry/trash prefix stops the purge, it does not fail the tick
        out.reason = f"the maintained estate or the trash listing could not be read ({type(exc).__name__}: {exc}) — refusing to purge on a partial map"
        log.error("trash_purge_blocked", extra={"reason": out.reason})
        return out

    out.ran = True
    out.due = len(due)
    if len(due) > settings.trash_purge_max_per_tick:
        out.capped = len(due) - settings.trash_purge_max_per_tick
        due = due[: settings.trash_purge_max_per_tick]

    # One manifest read per root per tick, shared by every record — and `None` (unreadable) refuses all
    # of them rather than letting a broken index read as "nothing is live".
    live_ids = await run_in_threadpool(live_ids_across, roots, storage_options) if due else set()
    for record in due:
        await _purge_one(
            record,
            out,
            settings=settings,
            storage_options=storage_options,
            control_root=resolved_control_root,
            roots=roots,
            live_ids=live_ids,
            fga_client=fga_client,
            control=control,
        )

    _record_metrics(out)
    log.info(
        "trash_purge",
        extra={
            "due": out.due,
            "purged": len(out.purged),
            "refused": len(out.refused),
            "capped": out.capped,
            "bytes_reclaimed": sum(p.bytes_deleted for p in out.purged),
            "skipped_categories": out.skipped_categories,
        },
    )
    return out


def _record_metrics(out: TrashPurgeReport) -> None:
    """Emit the reclamation counters — always, including the zeroes, so the series exist from tick one."""
    purged: dict[str, int] = {}
    refused: dict[str, int] = {}
    for entry in out.purged:
        purged[entry.kind] = purged.get(entry.kind, 0) + 1
    for refusal in out.refused:
        refused[refusal.kind] = refused.get(refusal.kind, 0) + 1
    record_trash_purge(purged, refused, sum(p.bytes_deleted for p in out.purged))
