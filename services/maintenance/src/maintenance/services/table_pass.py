"""One table's maintenance pass, run for the task the work queue hands one replica (ADR 0198 §2, §3).

Maintenance decides and does not rewrite (ADR 0155, ADR 0156 C4 and C6, ADR 0162). A table's task is put on the queue
when the catalog makes the table, when a commit lands on it, and when its next due time comes
(:mod:`maintenance.services.queue`); one replica's worker runs this pass for it, and no other runs one for the same table
meanwhile. The pass:

1. **Is it still the table the task names.** Its location's claim must still record the task's uuid, and no trash record
   may keep it. A table gone or in the trash gets no pass and no next time, and its due record goes; a trashed table's
   own trash task decides what follows. A table in a bucket maintenance does not govern (a deactivated warehouse's), and
   a declared table, which holds no dataset until its first commit, are read no further and wait for the recheck.
2. **Names and plans it** from the ``__manifest`` of its own bucket and the policy registry, as every maintenance reader
   names a table ([[LH-520]]).
3. **Per dataset**, the table then each of its branches: version expiry under a table-scoped vend
   (:func:`maintenance.services.expiry.expire_versions`); then, on the table, its due record (:mod:`maintenance.services.due`)
   and its committed files (:mod:`maintenance.services.committed_files`).
4. **When it next needs a pass** (:func:`next_due`), read again after the pass, which the worker schedules.

What a pass costs follows the one table, never the estate: its claim, its trash record, its bucket's ``__manifest``, the
policies, and its own datasets. Blocking S3/Lance work; the worker runs :func:`run_table_pass` in the threadpool.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from time import perf_counter
from typing import Final, Literal

import lance
from botocore.exceptions import BotoCoreError, ClientError
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, ConfigDict, Field

from lakehouse import auto_cleanup, base_refs, base_registry, data_directories, location_claims, maintenance_policies, objectfs, trash, warehouse_records
from lakehouse.base_refs import decoded_path, key_uri, store_of
from lakehouse.branch_layout import BRANCH_CONTAINER
from lakehouse.features import flags_from_open_error, manifest_base_path_refs, manifest_feature_flags, mixes_data_file_versions
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.history_fences import TableHistory
from lakehouse.lance_session import affordable_cache_bytes
from lakehouse.lancekit.versions import committed_at
from lakehouse.maintenance_due import delete_due
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings, get_settings, shared_lance_session
from maintenance.core.lineage_emit import MaintenanceEmitter
from maintenance.core.metrics import (
    record_base_fragments_holding_deleted_rows,
    record_dataset_swept,
    record_failed,
    record_mixed_file_versions,
    record_overwritten_files,
    record_reclaimed,
    record_refused,
    record_table_parked,
    record_unrecorded_bases,
)
from maintenance.services import base_reclaim, committed_files, credentials
from maintenance.services.due import refresh_due, table_ids_of
from maintenance.services.expiry import HISTORY_FENCED, DatasetResult, ExpiryPolicy, ExpiryTarget, count_reclaimable, expire_versions, lost_staged_manifests
from maintenance.services.naming import PolicyOwners, TableIds, claim_store, policy_owners, read_uuids
from maintenance.services.plan import DatasetPlan, PlanContext, cadence_skip, resolve_plan


log = logging.getLogger(__name__)

tracer = trace.get_tracer(__name__)

#: The soonest a pass schedules the next one, so a table that stays due through a pass is not passed in a hot loop.
_SOONEST: Final = timedelta(seconds=60)


class TableTask(BaseModel):
    """One table's pass, as its task names it: the uuid the catalog minted for it, and the location its claim records."""

    model_config = ConfigDict(frozen=True)

    uuid: str
    location: str


class PassOutcome(BaseModel):
    """What one table's pass did, and when the table next needs one."""

    #: One result per dataset the pass ran on: the table, then each branch.
    results: list[DatasetResult] = Field(default_factory=list)
    #: When the table next needs a pass; ``None`` when nothing is to be scheduled, because the table is gone or trashed.
    next_due: datetime | None = None
    #: Why no pass ran: the claim no longer records the task's uuid (``gone``), or a trash record keeps the table.
    ended: Literal["gone", "trashed"] | None = None


class PassReadings(BaseModel):
    """What a pass reads once before it touches a dataset: the ids its bucket's ``__manifest`` holds, the uuids its
    table's policy is kept under, and the policies and clock its plan is resolved under."""

    table_ids: TableIds
    context: PlanContext
    #: The table the pass is of, and the uuids its policy was read under; ``None`` when they could not be read, which
    #: refuses each of its datasets. Read once, so the policy in ``context`` is the one these uuids name: a second read
    #: that succeeded where the first failed would name the table with no policy read for it.
    table: str | None = None
    owners: PolicyOwners | None = None


class NamedDataset(BaseModel):
    """The table a dataset belongs to, as ``__manifest`` names it, and the uuids whose records may govern it."""

    table: str
    owners: PolicyOwners


class WriteProbe(BaseModel):
    """What one pre-vend open of a dataset tells us: whether expiry could write, and the manifest facts it reports."""

    may_write: bool
    data_storage_version: str | None = None
    mixed_data_file_versions: bool = False


def emit_zero_baselines() -> None:
    """Create every per-dataset series at 0 when the worker starts: an estate whose tables pass nothing yet runs no
    dataset, and a counter nothing adds to is a series that does not exist, so an alert on it reads "no data"."""
    record_reclaimed(versions_removed=0)
    record_refused(0)
    record_mixed_file_versions(0)
    record_overwritten_files(0)
    record_unrecorded_bases(0)


def base_record_reader(settings: MaintenanceSettings, options: StorageOptions) -> Callable[[str], base_registry.BaseRecord | None]:
    """How this service reads the catalog's base record for a table root ([[LH-279]]).

    The reader RAISES on a record it cannot read: answering "no record" would hand a table's data directories to a pass
    that cannot tell which of them it holds.
    """
    registry = base_registry.BaseRegistry(control_root=settings.resolved_control_root, storage_options=options)
    return partial(base_registry.read_base_record, registry)


def _probe_before_vending(
    uri: str, options: StorageOptions, *, policy: ExpiryPolicy, directories: list[base_registry.RecordedBase], now: datetime
) -> WriteProbe:
    """Could expiry write anything at all on this dataset?

    THE COST IT REMOVES, measured 2026-09-11: the store mints one STS identity record per vend and prunes none — 280 per
    minute on this estate — and the previous store became unable to restart at 107,485 of them. Expiry writes only
    when commit-path cleanup is armed (the disarm commits), when the policy's threshold of reclaimable versions is met,
    counted by the same rule the pass itself applies, or when the table's data directories hold a file the pass would
    delete ([[LH-286]]), found by the rule the pass applies again under its vend (`base_reclaim.unreferenced`). A held
    table, whose policy keeps its whole history, is written nothing, its disarm included: the catalog vends it no
    maintain tier ([[XC-084]]), and the cleanup it may be armed with fires only inside a commit, which every catalog
    door disarms before it makes (`lakehouse.auto_cleanup`). A manifest a stock writer staged for a version the table
    already holds, past its grace, is one more thing to write: the pass removes it whether or not any version is due
    ([[LH-529]]), and a pass that could not would find the table due again a minute later.

    CONSERVATIVE BY CONSTRUCTION: an unreadable dataset answers `True`, because skipping a dataset that WOULD have been
    maintained is the failure this service exists to prevent while a spare credential is only a cost. Logged, never
    silent: the conservative answer otherwise hides a real fault as an extra credential.
    """
    try:
        ds = lance.dataset(uri, storage_options=options.in_clear(), session=shared_lance_session())
        armed = policy.cleanup_enabled and bool(auto_cleanup.armed_keys(ds.config()))
        reclaimable = policy.cleanup_enabled and count_reclaimable(ds, policy) >= policy.min_reclaimable
        stranded = policy.cleanup_enabled and bool(directories) and bool(base_reclaim.unreferenced(ds, directories, options, now=now))
        lost = policy.cleanup_enabled and any(modified <= now - policy.staged_grace for _, modified in lost_staged_manifests(uri, options, version=ds.version))
        return WriteProbe(
            may_write=armed or reclaimable or stranded or lost,
            data_storage_version=ds.data_storage_version,
            mixed_data_file_versions=mixes_data_file_versions(manifest_feature_flags(ds)[0]),
        )
    except Exception as exc:
        log.debug("maintenance_write_probe_unreadable", extra={"uri": uri, "error": f"{type(exc).__name__}: {exc}"})
        return WriteProbe(may_write=True, mixed_data_file_versions=mixes_data_file_versions(flags_from_open_error(exc) or 0))


def _on_governed_elsewhere(result: DatasetResult, exc: Exception, _table_id: str | None) -> None:
    result.refuse(str(exc), gate="governed_elsewhere")


def _on_unauthenticated(result: DatasetResult, exc: Exception, _table_id: str | None) -> None:
    result.refuse(str(exc), gate="unauthenticated")


def _on_vending_not_direct(result: DatasetResult, exc: Exception, _table_id: str | None) -> None:
    result.refuse(str(exc), gate="vending_not_direct")


def _on_vend_denied(result: DatasetResult, exc: Exception, table_id: str | None) -> None:
    result.park(str(exc), gate="vend_denied", table_id=table_id)


def _on_table_not_governed(result: DatasetResult, exc: Exception, table_id: str | None) -> None:
    result.park(str(exc), gate="table_not_governed", table_id=table_id)


def _on_vend_undecided(result: DatasetResult, exc: Exception, _table_id: str | None) -> None:
    result.error, result.error_type = f"maintain: {exc}", type(exc).__name__


#: How each answer of the vend door that stops the dataset is recorded, and the line it logs. A refusal and a park are
#: not failures: nothing broke, a grant is missing, a stamp is wrong, or this deployment vends no direct credential.
#: Only an undecided door is a failure, retried at the next pass. Every one of them stops the dataset: a rewrite is
#: signed by its table's vend or not at all ([[LH-219]]).
_VEND_ANSWERS: dict[type[Exception], tuple[str, Callable[[DatasetResult, Exception, str | None], None]]] = {
    credentials.GovernedElsewhere: ("maintenance_governed_elsewhere", _on_governed_elsewhere),
    credentials.MaintenanceUnauthenticated: ("maintenance_unauthenticated", _on_unauthenticated),
    credentials.VendingNotDirect: ("maintenance_vending_not_direct", _on_vending_not_direct),
    credentials.MaintenanceDenied: ("maintenance_vend_denied", _on_vend_denied),
    credentials.TableNotGoverned: ("maintenance_table_not_governed", _on_table_not_governed),
    credentials.VendUndecided: ("maintenance_vend_undecided", _on_vend_undecided),
}


#: Every answer of the vend door that stops a write, caught as one.
_VEND_STOPS: Final = tuple(_VEND_ANSWERS)


def _record_vend_answer(result: DatasetResult, exc: Exception, *, table_id: str | None) -> None:
    """Log the vend door's answer that stopped a write, and record it on ``result`` unless the result already carries
    the pass's refusal or error, which a later ask of the same door must not replace."""
    event, record = _VEND_ANSWERS[type(exc)]
    log.warning(event, extra={"uri": result.uri, "table_id": table_id, "reason": str(exc)})
    if result.refused is None and result.error is None:
        record(result, exc, table_id)


def _stopped_by_vend(probe: WriteProbe, target: ExpiryTarget, exc: Exception) -> DatasetResult:
    """The result of a dataset the vend door's answer stopped, carrying the census the probe already read."""
    stopped = DatasetResult(uri=target.uri, data_storage_version=probe.data_storage_version, mixed_data_file_versions=probe.mixed_data_file_versions)
    _record_vend_answer(stopped, exc, table_id=target.table_id)
    return stopped


def _data_directories(uri: str, *, readings: PassReadings, settings: MaintenanceSettings, options: StorageOptions) -> list[base_registry.RecordedBase]:
    """The data directories the table at ``uri`` holds, as its base record names them ([[LH-286]]).

    None for a branch, whose files are its table's, and none for a table whose manifest declares no plain base outside
    its own root: the record is read only for a table that could hold a directory. A record that cannot be read names
    none this pass, so the pass reclaims nothing on the base and expires the table's versions as usual; the next pass
    reads it again. Only a directory the record names and whose claim the table holds is ever listed, so neither a base a
    writer planted in the manifest nor one another table holds is reclaimed.
    """
    if (table := readings.table_ids.of(uri)) is None:
        return []
    try:
        declared = manifest_base_path_refs(lance.dataset(uri, storage_options=options.in_clear(), session=shared_lance_session()))
    except Exception as exc:  # noqa: BLE001 — the pass opens the dataset again and reports its own failure
        log.debug("maintenance_data_directories_unopened", extra={"uri": uri, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return []
    if not any(not ref.is_dataset_root and not base_refs.location_in_store(uri, ref.path) for ref in declared):
        return []
    try:
        recorded = data_directories.recorded_directories(base_record_reader(settings, options)(uri))
        held = [directory for directory in recorded if data_directories.claimed_by(claim_store(settings), directory, table)]
    except (base_registry.UnreadableBaseRecordError, ClientError, BotoCoreError, OSError, ValueError) as exc:
        log.warning("maintenance_base_record_unreadable", extra={"uri": uri, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return []
    if unheld := [directory.path for directory in recorded if directory not in held]:
        log.warning("maintenance_data_directory_not_held", extra={"uri": uri, "table": table, "directories": unheld})
    return held


def _expire(uri: str, named: NamedDataset, plan: DatasetPlan, *, readings: PassReadings, settings: MaintenanceSettings) -> DatasetResult:
    """One dataset's expiry: read under maintenance's own session, and written only under a vend scoped to the table
    ``__manifest`` names it as ([[LH-219]]), asked for where the probe finds something to write.

    One name feeds the vend and the result, so the table a credential is scoped to and the table events name cannot
    disagree. The pass holds that table's history fence, keyed by its uuid and written under the vend, while it deletes
    ([[LH-308]]), so a pass over a branch holds the same fence as a pass over its table.
    """
    table = named.table
    options = settings.storage_options()
    policy = plan.expiry_policy(min_reclaimable=settings.min_versions_to_expire, staged_grace=timedelta(seconds=settings.staged_manifest_grace_seconds))
    directories = _data_directories(uri, readings=readings, settings=settings, options=options)
    now = readings.context.now
    probe = _probe_before_vending(uri, options, policy=policy, directories=directories, now=now)
    target = ExpiryTarget(uri=uri, storage_options=options, table_id=table, data_directories=directories, now=now)
    if not probe.may_write:
        return expire_versions(target, policy)
    try:
        vended = credentials.write_options_for(uri, settings, table_id=table)
    except _VEND_STOPS as exc:
        return _stopped_by_vend(probe, target, exc)
    history = TableHistory(control_root=settings.resolved_control_root, storage_options=vended, table_uuid=named.owners.table_uuid)
    return expire_versions(target.model_copy(update={"write_options": vended, "history": history}), policy)


def _expire_in_span(uri: str, named: NamedDataset, plan: DatasetPlan, *, readings: PassReadings, settings: MaintenanceSettings) -> DatasetResult:
    """:func:`_expire` inside its own span, timed around the work: a failure and a refusal are both on the span.

    A pass its lineage event will name carries the uuids of the table and its namespace, read only for such a pass.
    """
    with tracer.start_as_current_span("maintenance.expire_versions") as span:
        span.set_attribute("lance.maintenance.dataset_uri", uri)
        started = perf_counter()
        result = _expire(uri, named, plan, readings=readings, settings=settings)
        if _lane_of(result) is not None:
            result.uuids = read_uuids(readings.table_ids, named.table, claims=claim_store(settings))
        result.duration_seconds = round(perf_counter() - started, 3)
        span.set_attribute("lance.maintenance.duration_seconds", result.duration_seconds)
        if result.refused is not None:
            span.set_attribute("lance.maintenance.refused", result.refused)
        if result.error is not None:
            span.set_status(StatusCode.ERROR, result.error)
            span.set_attribute("error.type", result.error_type or "Unknown")
        return result


def name_dataset(uri: str, *, readings: PassReadings, settings: MaintenanceSettings) -> NamedDataset | DatasetResult:
    """The table ``uri`` belongs to with its policy owners, or the refusal of a dataset maintenance cannot identify."""
    table = readings.table_ids.table_of(uri)
    if table is None:
        return _unnamed(uri, settings=settings)
    if table != readings.table or readings.owners is None:
        return _unidentified(uri, table, settings=settings)
    return NamedDataset(table=table, owners=readings.owners)


def _plan_for(uri: str, named: NamedDataset, *, readings: PassReadings, settings: MaintenanceSettings) -> DatasetPlan:
    """The dataset's plan, with the policy cadence applied."""
    plan = resolve_plan(uri, context=readings.context, owners=named.owners)
    if plan.policy is not None and (
        skipped := cadence_skip(plan.policy, settings=settings, table_uuid=named.owners.table_uuid, uri=uri, now=readings.context.now)
    ):
        plan.skipped = skipped
    return plan


def maintain_dataset(uri: str, *, readings: PassReadings, settings: MaintenanceSettings) -> DatasetResult:
    """One dataset's pass: expiry, then on the table its due record and committed files, then its cadence stamp and
    counters."""
    record_dataset_swept()
    named = name_dataset(uri, readings=readings, settings=settings)
    if isinstance(named, DatasetResult):
        return named
    plan = _plan_for(uri, named, readings=readings, settings=settings)
    if plan.skipped is not None:
        return _skipped(uri, plan, named, readings=readings, settings=settings)
    result = _expire_in_span(uri, named, plan, readings=readings, settings=settings)
    if readings.table_ids.of(uri) is not None:
        question = plan.due_question(table=named.table, uuid=named.owners.table_uuid, location=uri)
        result.due = refresh_due(question, settings, now=readings.context.now)
        _check_committed_files(result, named, settings=settings)
        _report_unrecorded_bases(uri, settings=settings)
    else:
        result.due = "unnamed"
    _finish(result, named=named, plan=plan, now=readings.context.now, settings=settings)
    return result


def _check_committed_files(result: DatasetResult, named: NamedDataset, *, settings: MaintenanceSettings) -> None:
    """Name each file a commit added that the store no longer holds as the catalog recorded it ([[LH-419]]), and forget
    each record no retained version needs.

    On the table's own pass only: a branch's files are recorded under its table, whose pass reads them. Whatever the
    expiry did, so a table the expiry refuses is still read, and on a pass the policy cadence skips too. The read is under
    maintenance's own session; forgetting is a write, signed by the table's vend ([[LH-219]]), asked for only when a
    record is reclaimed.
    """
    options = settings.storage_options()
    try:
        found = committed_files.check_and_copy(
            result.uri,
            named.owners.table_uuid,
            open_table=lambda: lance.dataset(result.uri, storage_options=options.in_clear(), session=shared_lance_session()),
            data_options=options,
            control_root=settings.resolved_control_root,
            control_options=options,
        )
    except (OSError, ValueError) as exc:
        log.warning("committed_files_unchecked", extra={"uri": result.uri, "table_id": result.table_id, "error": f"{type(exc).__name__}: {exc}"[:300]})
        if result.error is None:
            result.error, result.error_type = f"committed files: {exc}", type(exc).__name__
        return
    result.overwritten_files = found.overwritten
    if found.reclaimed:
        _forget_reclaimed(result, table=named.table, table_uuid=named.owners.table_uuid, reclaimed=found.reclaimed, settings=settings)


def _report_unrecorded_bases(uri: str, *, settings: MaintenanceSettings) -> None:
    """Count and name each base the table's manifest declares that nothing sanctions ([[LH-279]]).

    A planted base protects nothing and every door refuses it (`base_registry.judge_bases`, the predicate the vend, read
    and register doors share); this is where an operator sees one. A base inside the table's own root, or a configured
    external blob base, is sanctioned without a read, so the common table reads no base record. An unreadable manifest
    or record reports nothing this pass.
    """
    options = settings.storage_options()
    try:
        declared = manifest_base_path_refs(lance.dataset(uri, storage_options=options.in_clear(), session=shared_lance_session()))
        judged = base_registry.judge_bases(
            uri, declared, configured=settings.external_blob_base_list, load_record=lambda: base_record_reader(settings, options)(uri)
        )
    except (base_registry.UnreadableBaseRecordError, ClientError, BotoCoreError, OSError, ValueError) as exc:
        log.warning("maintenance_bases_unjudged", extra={"uri": uri, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return
    unrecorded = [decoded_path(judgement.ref.path) for judgement in judged if judgement.standing is base_registry.BaseStanding.UNRECORDED]
    record_unrecorded_bases(len(unrecorded))
    if unrecorded:
        log.warning("maintenance_unrecorded_base", extra={"referrer": uri, "bases": unrecorded})


def _forget_reclaimed(result: DatasetResult, *, table: str, table_uuid: str, reclaimed: list[str], settings: MaintenanceSettings) -> None:
    """Forget the ``reclaimed`` commits' records under the table's vend ([[LH-219]]).

    A record no retained version needs governs nothing, so an undecided vend or a failed delete leaves it for a later
    pass and fails nothing: the versions the pass reclaimed stand. A refused vend is a refusal, recorded on the result as
    the expiry records its own.
    """
    try:
        vended = credentials.write_options_for(result.uri, settings, table_id=table)
        committed_files.forget(settings.resolved_control_root, vended, table_uuid=table_uuid, reclaimed=reclaimed)
    except (credentials.VendUndecided, OSError) as exc:
        log.warning("committed_files_not_forgotten", extra={"uri": result.uri, "table_id": table, "error": f"{type(exc).__name__}: {exc}"[:300]})
    except _VEND_STOPS as exc:
        _record_vend_answer(result, exc, table_id=table)


def _unnamed(uri: str, *, settings: MaintenanceSettings) -> DatasetResult:
    """A dataset no ``__manifest`` row names, reported by its location and never expired or vended ([[LH-520]]).

    Maintenance acts only on a table the catalog names: a dataset the catalog cannot name has no table a vend could be
    scoped to, and its own credential would sign the pass instead. Its location is the whole report, since nothing
    names it otherwise.
    """
    refused = DatasetResult(uri=uri, due="unnamed").refuse(
        f"no __manifest row names the dataset at {uri}, so maintenance neither expires nor vends it", gate="unnamed"
    )
    _record_dataset_metrics(refused)
    _record_dataset_outcome(refused, subject=settings.catalog_service_identity)
    return refused


def _unidentified(uri: str, table: str, *, settings: MaintenanceSettings) -> DatasetResult:
    """A table whose uuid, or an ancestor namespace's, cannot be read: refused this pass rather than maintained under the
    defaults, since the policy kept under that uuid may be a legal hold ([[LH-523]])."""
    refused = DatasetResult(uri=uri, due="failed").refuse(
        f"the uuid of table {table} or of a namespace above it cannot be read, so its maintenance policy cannot be found", gate="unidentified"
    )
    _record_dataset_metrics(refused)
    _record_dataset_outcome(refused, subject=settings.catalog_service_identity)
    return refused


def _skipped(uri: str, plan: DatasetPlan, named: NamedDataset, *, readings: PassReadings, settings: MaintenanceSettings) -> DatasetResult:
    """A pass the policy cadence skips: no expiry, but the table's recorded files are read all the same, since a store
    event holds this pass for them and the read costs one listing."""
    skipped = DatasetResult(uri=uri, skipped=plan.skipped)
    if readings.table_ids.of(uri) is not None:
        _check_committed_files(skipped, named, settings=settings)
    record_overwritten_files(len(skipped.overwritten_files))
    _record_dataset_outcome(skipped, subject=settings.catalog_service_identity)
    return skipped


def _finish(result: DatasetResult, *, named: NamedDataset, plan: DatasetPlan, now: datetime, settings: MaintenanceSettings) -> None:
    _stamp_cadence(result, table_uuid=named.owners.table_uuid, plan=plan, settings=settings, now=now)
    _record_dataset_metrics(result)
    _record_dataset_outcome(result, subject=settings.catalog_service_identity)


def _stamp_cadence(result: DatasetResult, *, table_uuid: str, plan: DatasetPlan, settings: MaintenanceSettings, now: datetime) -> None:
    """Record this dataset's ``last_maintained_at`` — only after a pass that could actually DO something.

    A refusal carries ``error=None``, so without ``refused is None`` a dataset no pass can ever maintain would read as a
    transient ``policy_interval`` skip for the whole interval rather than a standing refusal.
    """
    policy = plan.policy
    if policy is None or not policy.get("compact_interval_hours") or result.error is not None or result.refused is not None or result.skipped is not None:
        return
    try:
        maintenance_policies.write_state(
            settings.resolved_policy_root, settings.storage_options(), policy, table_uuid=table_uuid, uri=result.uri, at=now.isoformat()
        )
    except Exception as exc:  # noqa: BLE001 — a lost stamp re-paces one dataset; it must not fail the pass
        log.warning("compaction_policy_stamp_failed", extra={"policy": policy.get("id"), "uri": result.uri, "error": str(exc)})


def _record_dataset_metrics(result: DatasetResult) -> None:
    """The per-dataset counters, keyed by the STABLE error class and the refusing gate, never a message or a URI."""
    # A base file is a byte a superseded version stranded, so the bytes a pass frees count both ([[LH-286]]).
    record_reclaimed(versions_removed=result.old_versions_removed, bytes_removed=result.bytes_removed + result.base_bytes_removed)
    record_base_fragments_holding_deleted_rows(result.base_fragments_holding_deleted_rows)
    record_refused(1 if result.refused else 0, result.refused_by)
    _record_parked(result)
    record_mixed_file_versions(1 if result.mixed_data_file_versions else 0)
    record_overwritten_files(len(result.overwritten_files))
    if result.error is not None:
        record_failed({result.error_type or "Unknown": 1})


def _record_parked(result: DatasetResult) -> None:
    """Count a catalog refusal under the table id it refused, which `MaintenanceTableParked` pages on. Only the
    catalog's answers: a layout refusal can land on a location that names no table."""
    if result.refused_table_id is None:
        return
    match result.refused_by:
        case "vend_denied" | "table_not_governed" as gate:
            record_table_parked(table_id=result.refused_table_id, refused_by=gate)
        case _:
            return


def _record_dataset_outcome(result: DatasetResult, *, subject: str) -> None:
    """One structured line per dataset per pass, the uneventful ones included: a dataset a pass looked at and left alone
    is the answer to a real question. The fields ride `extra=`, so they land in GreptimeDB as `log_attributes`:

        SELECT log_attributes FROM opentelemetry_logs WHERE body = 'maintenance_dataset_outcome'
    """
    log.info(
        "maintenance_dataset_outcome",
        extra={
            "dataset": result.uri,
            "table_id": result.table_id,
            "old_versions_removed": result.old_versions_removed,
            "bytes_removed": result.bytes_removed,
            "staged_manifests_removed": result.staged_manifests_removed,
            "base_files_removed": result.base_files_removed,
            "base_bytes_removed": result.base_bytes_removed,
            "base_fragments_holding_deleted_rows": result.base_fragments_holding_deleted_rows,
            "due": result.due,
            "refused": result.refused,
            "skipped": result.skipped,
            "error": result.error,
            "error_type": result.error_type,
            "data_storage_version": result.data_storage_version,
            "mixed_data_file_versions": result.mixed_data_file_versions,
            "overwritten_files": result.overwritten_files,
        },
    )
    audit_material_work(result, subject=subject)


def _did_material_work(result: DatasetResult) -> bool:
    """Did the pass reclaim any version, or any file in the table's data directories? Only then is a maintenance run
    recorded: a pass on every commit would otherwise flood the lineage graph and the compliance trail with no-op runs."""
    return result.old_versions_removed > 0 or result.base_files_removed > 0


def audit_material_work(result: DatasetResult, *, subject: str) -> None:
    """Record a reclamation on the `lance.audit` compliance stream, keyed on the object it reclaimed.

    The RESOURCE is the FGA object (`table:<id>`), not the URI: a compliance query joins on the object the rest of the
    trail names. A dataset with no id is recorded under its URI, since the reclamation still happened.
    """
    if not _did_material_work(result):
        return
    audit(
        AuditAction.EXPIRE_VERSIONS,
        AuditOutcome.SUCCESS,
        subject=subject,
        resource=f"table:{result.table_id}" if result.table_id else result.uri,
        dataset=result.uri,
        old_versions_removed=result.old_versions_removed,
        bytes_removed=result.bytes_removed,
        base_files_removed=result.base_files_removed,
        base_bytes_removed=result.base_bytes_removed,
        duration_seconds=result.duration_seconds,
    )


def _lane_of(result: DatasetResult) -> Literal["complete", "fail"] | None:
    """The lineage event a pass emits: ``fail`` for a ``maintain:`` failure, ``complete`` for reclaimed versions, and
    none for an ``open:`` error, a refusal or a dataset no id names."""
    error = result.error or ""
    if result.table_id is None:
        return None
    if error.startswith("maintain:"):
        return "fail"
    if not error.startswith("open:") and _did_material_work(result):
        return "complete"
    return None


async def emit_pass_lineage(emitter: MaintenanceEmitter, results: list[DatasetResult]) -> None:
    """Emit a best-effort maintenance event per dataset of one pass: COMPLETE for reclaimed versions, FAIL for a
    terminal failure (#7b + §4 failure visibility). Nothing here raises into the pass.

    Each names the table by its catalog id under the catalog's dataset namespace, as the catalog's own events do, so the
    pass lands on the SAME dataset the catalog created.
    """
    for result in results:
        lane = _lane_of(result)
        if lane is None or result.table_id is None:
            continue
        try:
            if lane == "complete":
                await emitter.emit_maintenance(table_id=result.table_id, location=result.uri, uuids=result.uuids)
            else:
                await emitter.emit_maintenance_failed(table_id=result.table_id, location=result.uri, error=result.error or "", uuids=result.uuids)
        except Exception as exc:  # noqa: BLE001 — the emitter is best-effort; a publish failure never fails the pass
            log.warning(f"maintenance_{lane}_emit_error", extra={"table": result.table_id, "error": str(exc)})


def governs(location: str, *, top_namespace: str, settings: MaintenanceSettings, options: StorageOptions) -> bool | None:
    """Whether the table at ``location``, under the top-level namespace ``top_namespace``, is one maintenance governs: in a
    configured bucket, or in its namespace's warehouse while that warehouse is active (#81); ``None`` when the warehouse
    cannot be read, which decides nothing.

    A deactivated warehouse's tables are not governed: deactivate is offboarding step one, and maintenance must not be the
    one process still rewriting a quarantined tenant's data. The warehouse is the namespace's, read through its binding by
    key, so what the pass costs is the table's, never the registry's; and two warehouses one project backs with one bucket
    (the work and gold pair) are each judged by their own status. A location outside S3 is the control root's own.
    """
    if not location.startswith("s3://"):
        return True
    if location.removeprefix("s3://").split("/", 1)[0] in settings.sweep_buckets:
        return True
    try:
        warehouse = warehouse_records.namespace_warehouse(settings.resolved_control_root, options, top_namespace)
    except (OSError, ValueError) as exc:
        log.warning("maintenance_warehouse_unreadable", extra={"location": location, "namespace": top_namespace, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return None
    return warehouse is not None and not warehouse_records.is_deactivated(warehouse)


def read_for(location: str, held: location_claims.LocationClaim, settings: MaintenanceSettings, *, now: datetime) -> PassReadings | None:
    """What a pass on the table ``held`` names at ``location`` reads before touching it, or ``None`` when no decision is
    possible.

    What it costs in requests is the table's, never the estate's: the rows of the table and of each namespace above it
    in the ``__manifest`` of the bucket ``location`` is in, one filtered read of an index Lance keeps unindexed, so its
    bytes grow with the bucket's rows while its requests do not (:func:`maintenance.services.purge.manifest_rows_named`);
    and, by key, the one policy record that governs the table (:func:`lakehouse.maintenance_policies.governing_policy`).
    A manifest or a policy record that cannot be read decides nothing (``None``): the policies are the protective
    surface, and expiring without them would reclaim history an owner kept. A table whose policy owners cannot be read
    (:func:`maintenance.services.naming.policy_owners`) leaves the policy unread and the readings without owners, so each
    of its datasets is refused (:func:`name_dataset`).
    """
    options = settings.storage_options()
    root = f"s3://{location.removeprefix('s3://').split('/', 1)[0]}" if location.startswith("s3://") else settings.resolved_control_root
    ids = table_ids_of(root, options, segments=held.segments)
    if ids is None:
        return None
    policies: list[dict[str, object]] = []
    owners = policy_owners(ids, held.table, claims=claim_store(settings))
    if owners is not None:
        try:
            policy = maintenance_policies.governing_policy(
                settings.resolved_policy_root,
                options,
                claims_root=settings.resolved_control_root,
                location=location,
                table_uuid=owners.table_uuid,
                namespace_uuids=owners.namespace_uuids,
            )
        except maintenance_policies.UnreadablePolicyError as exc:
            log.error("maintenance_policy_unreadable", extra={"uuid": held.uuid, "table": held.table, "error": str(exc)[:300]})
            return None
        policies = [policy] if policy is not None else []
    context = PlanContext(policies=policies, now=now, older_than=timedelta(days=settings.older_than_days))
    return PassReadings(table_ids=ids, context=context, table=held.table, owners=owners)


def _held_location(location: str) -> str:
    """Where the store keeps the table at ``location``: the key a percent-encoded registered location decodes to
    ([[LH-319]]), in the store its spelling names, as discovery and every maintenance reader hold it."""
    return key_uri(store_of(location), decoded_path(location))


def _datasets_of(location: str, settings: MaintenanceSettings) -> list[str] | None:
    """The table at ``location``, the key its store holds it under, and each of its branches, read from its refs, never by
    walking ``tree/``; ``None`` when no dataset stands there yet, as at a table declared and not yet written.

    A branch's directory is ``<table>/tree/<name>`` (`lance_docs/lance/format/table/branch_tag.md`). Refs that cannot be
    read leave the table alone, and the next pass reads them again. Whether a dataset stands is the store's answer, a
    ``_versions/`` child (:func:`lakehouse.objectfs.is_lance_dataset_root`), asked only when the open failed.

    Raises:
        OSError: The open failed and the store could not say whether a dataset stands there, so nothing is decided.
    """
    options = settings.storage_options()
    try:
        dataset = lance.dataset(location, storage_options=options.in_clear(), session=shared_lance_session())
    except Exception as exc:  # noqa: BLE001 — the store's own answer below decides between an absent table and a fault
        if not objectfs.is_lance_dataset_root(location, options):
            return None
        log.warning("maintenance_table_unopened", extra={"uri": location, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return [location]
    try:
        branches = sorted(dataset.branches.list())
    except Exception as exc:  # noqa: BLE001 — the table's own pass opens it again and reports its own failure
        log.warning("maintenance_branches_unread", extra={"uri": location, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return [location]
    return [location, *(f"{location.rstrip('/')}/{BRANCH_CONTAINER}/{name}" for name in branches)]


def run_table_pass(task: TableTask, settings: MaintenanceSettings, *, now: datetime | None = None) -> PassOutcome:
    """One table's pass: the table and each branch, then when it next needs one. Never raises for one dataset's failure,
    which its result carries; a store that cannot be read to decide whether the task's table still stands raises, so the
    worker retries the task."""
    now = now or datetime.now(UTC)
    options = settings.storage_options()
    held = location_claims.holder(claim_store(settings), task.location)
    if held is None:
        log.info("maintenance_task_table_gone", extra={"uuid": task.uuid, "location": task.location})
        delete_due(settings.resolved_control_root, options, task.uuid)
        return PassOutcome(ended="gone")
    if held.uuid != task.uuid:
        # The location is another table's, so the task named a table by a place it is not at: nothing says the table it
        # names is gone, and its due record is left to its own pass.
        log.warning("maintenance_task_names_anothers_location", extra={"uuid": task.uuid, "location": task.location, "held_by": held.uuid})
        return PassOutcome(ended="gone")
    if trash.keeps(settings.resolved_control_root, options, task.uuid):
        log.info("maintenance_task_table_trashed", extra={"uuid": task.uuid, "table": held.table})
        delete_due(settings.resolved_control_root, options, task.uuid)
        return PassOutcome(ended="trashed")
    if (governed := governs(task.location, top_namespace=held.segments[0], settings=settings, options=options)) is not True:
        if governed is None:
            return PassOutcome(next_due=now + timedelta(seconds=settings.retry_seconds))
        log.warning("maintenance_task_outside_the_estate", extra={"uuid": task.uuid, "location": task.location})
        return PassOutcome(next_due=now + timedelta(hours=settings.recheck_hours))
    location = _held_location(task.location)
    datasets = _datasets_of(location, settings)
    if datasets is None:
        log.info("maintenance_task_table_unwritten", extra={"uuid": task.uuid, "table": held.table})
        return PassOutcome(next_due=now + timedelta(hours=settings.recheck_hours))
    readings = read_for(task.location, held, settings, now=now)
    if readings is None:
        return PassOutcome(next_due=now + timedelta(seconds=settings.retry_seconds))
    results = [maintain_dataset(uri, readings=readings, settings=settings) for uri in datasets]
    due = next_due(results, readings=readings, settings=settings)
    log.info("maintenance_table_pass", extra={"uuid": task.uuid, "table": held.table, "next_due": due.isoformat(), **memory_readings()})
    return PassOutcome(results=results, next_due=due)


def _reclaimable_at(ds: lance.LanceDataset, policy: ExpiryPolicy, *, older_than: timedelta, at: datetime, now: datetime) -> int:
    """How many versions the pass would reclaim at ``at``: Lance's own count, asked with the age ``older_than`` will
    then have ([[LH-562]])."""
    explained = ds.explain_cleanup_old_versions(older_than=older_than - (at - now), retain_versions=policy.retain_versions, error_if_tagged_old_versions=False)
    return explained.stats.old_versions


def versions_due_at(ds: lance.LanceDataset, policy: ExpiryPolicy, *, now: datetime) -> datetime | None:
    """When enough of this ref's versions are reclaimable for a pass to reclaim them, or ``None`` when only a commit can
    bring that about.

    The instant is the one the ``min_reclaimable``-th oldest superseded version passes ``older_than``, confirmed by Lance's
    own count at that instant, since a tag or a branch keeps versions no age releases: measured on pylance 13.0.0,
    `explain_cleanup_old_versions` asked with the age a version will have counts it, and leaves a tagged one out. When
    that count falls short, only a commit or the recheck finds out more. Keep-last-N has no instant: a version passes it
    only when a commit lands.
    """
    if not policy.cleanup_enabled or (older_than := policy.older_than) is None:
        return None
    superseded = sorted(committed_at(entry) for entry in ds.versions() if int(entry["version"]) != ds.version)
    if len(superseded) < policy.min_reclaimable:
        return None
    if _reclaimable_at(ds, policy, older_than=older_than, at=now, now=now) >= policy.min_reclaimable:
        return now
    upcoming = next((stamp + older_than for stamp in superseded[policy.min_reclaimable - 1 :] if stamp + older_than > now), None)
    if upcoming is None or _reclaimable_at(ds, policy, older_than=older_than, at=upcoming, now=now) < policy.min_reclaimable:
        return None
    return upcoming


def staged_due_at(ds: lance.LanceDataset, uri: str, *, policy: ExpiryPolicy, options: StorageOptions) -> datetime | None:
    """When the oldest manifest a stock writer staged for a version this ref already holds passes its grace ([[LH-529]]),
    or ``None`` when there is none, or the table's policy keeps its whole history, under which no pass removes one."""
    if not policy.cleanup_enabled:
        return None
    lost = [modified for _, modified in lost_staged_manifests(uri, options, version=ds.version)]
    return min(lost) + policy.staged_grace if lost else None


def _dataset_due_at(result: DatasetResult, readings: PassReadings, *, settings: MaintenanceSettings) -> datetime | None:
    """When the dataset ``result`` names next needs a pass, read afresh after this one: its versions, its staged
    manifests and its policy's cadence. A dataset the pass could not decide on is retried after ``retry_seconds``; a
    refused one, and one nothing comes due on, waits for its next commit or the recheck."""
    now = readings.context.now
    if result.error is not None or result.skipped == HISTORY_FENCED:
        return now + timedelta(seconds=settings.retry_seconds)
    if result.refused is not None:
        return None
    named = name_dataset(result.uri, readings=readings, settings=settings)
    if isinstance(named, DatasetResult):
        return None
    plan = resolve_plan(result.uri, context=readings.context, owners=named.owners)
    policy = plan.expiry_policy(min_reclaimable=settings.min_versions_to_expire, staged_grace=timedelta(seconds=settings.staged_manifest_grace_seconds))
    options = settings.storage_options()
    try:
        ds = lance.dataset(result.uri, storage_options=options.in_clear(), session=shared_lance_session())
        due = [at for at in (versions_due_at(ds, policy, now=now), staged_due_at(ds, result.uri, policy=policy, options=options)) if at is not None]
    except Exception as exc:  # noqa: BLE001 — an unread dataset is retried, as one the pass failed on
        log.warning("maintenance_next_due_unread", extra={"uri": result.uri, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return now + timedelta(seconds=settings.retry_seconds)
    earliest = min(due, default=None)
    if earliest is None or plan.policy is None or not (interval := plan.policy.get("compact_interval_hours")):
        return earliest
    stamped = cadence_stamp(plan.policy, settings=settings, table_uuid=named.owners.table_uuid, uri=result.uri)
    return max(earliest, stamped + timedelta(hours=int(str(interval)))) if stamped is not None else earliest


def cadence_stamp(policy: dict[str, object], *, settings: MaintenanceSettings, table_uuid: str, uri: str) -> datetime | None:
    """When the policy's cadence last let this dataset be maintained, or ``None`` when it never did or cannot tell."""
    try:
        stamped = maintenance_policies.read_state(settings.resolved_policy_root, settings.storage_options(), policy, table_uuid=table_uuid, uri=uri)
        return datetime.fromisoformat(stamped) if stamped else None
    except (OSError, ValueError, TypeError) as exc:
        log.warning("compaction_policy_state_unreadable", extra={"policy": policy.get("id"), "uri": uri, "error": str(exc)})
        return None


def next_due(results: list[DatasetResult], *, readings: PassReadings, settings: MaintenanceSettings) -> datetime:
    """When the table next needs a pass: the earliest any of its datasets does, never later than the recheck and never
    sooner than :data:`_SOONEST`.

    The recheck is the bound on what no event announces: a lost commit event, and the versions a deleted or moved tag or
    a deleted branch releases, which the catalog announces only on its control topic. It also bounds a table only the
    clock and a commit can change: one its policy keeps whole, one under keep-last-N, a refused one.
    """
    now = readings.context.now
    due = [at for result in results if (at := _dataset_due_at(result, readings, settings=settings)) is not None]
    return max(min([now + timedelta(hours=settings.recheck_hours), *due]), now + _SOONEST)


def memory_readings() -> dict[str, int]:
    """The three numbers [[LH-183]] turns on: ``python_blocks`` tracking RSS means Python retention, flat while RSS
    climbs means native allocation. ONE SEAM because they are one measurement; a subset reports nothing."""
    return {**_session_occupancy(), "python_blocks": _python_heap_blocks(), "rss_bytes": _rss_bytes()}


def _rss_bytes() -> int:
    """Resident set size of THIS process, from ``/proc/self/status``; ``-1`` where procfs is absent.

    ``VmRSS`` and not ``ru_maxrss``, which is a HIGH-WATER mark and so cannot tell a pass that grew and released from
    one that retained.
    """
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) << 10
    except (OSError, IndexError, ValueError):  # pragma: no cover - a platform without procfs
        return -1
    return -1


def _python_heap_blocks() -> int:
    """Blocks held by CPython's allocator. `sys.getallocatedblocks()` and not `len(gc.get_objects())`, which is blind
    to untracked atomic dicts (measured on CPython 3.13.12: 50,000 of them moved it by -188)."""
    return sys.getallocatedblocks()


def _session_occupancy() -> dict[str, int]:
    """The shared Lance session's current size and the cap it is growing toward, in bytes; ``-1`` when unreadable."""
    settings = get_settings()
    metadata, index = affordable_cache_bytes(settings.lance_metadata_cache_mb << 20, settings.lance_index_cache_mb << 20)
    try:
        # CALLED, not read: `Session.size_bytes` is a method on pylance 11.0.0.
        size = int(shared_lance_session().size_bytes())
    except (AttributeError, TypeError, ValueError):  # pragma: no cover - a pylance that moved the accessor
        size = -1
    return {"lance_session_bytes": size, "lance_session_cap_bytes": metadata + index}
