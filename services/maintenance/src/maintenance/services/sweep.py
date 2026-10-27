"""The maintenance sweep: discover every dataset, expire its versions, decide what needs compacting or reindexing.

Maintenance decides and does not rewrite (ADR 0155, ADR 0156 C4 and C6, ADR 0162). One serial lane per tick:

1. **Plan** — the policies and the trash exclusions (either unreadable ABORTS the tick: both are protective), every
   swept bucket, discovery, the whole-estate base-reference pre-pass, and the catalog's table ids.
2. **The clone list**, written before any due record, so a job reading a record is judged against the pre-pass that
   wrote it.
3. **Per dataset**, inside the tick's budget: version expiry under a table-scoped vend
   (:func:`maintenance.services.expiry.expire_versions`), then the due evaluation that writes or deletes the table's
   record (:mod:`maintenance.services.due`).
4. **The due records of tables no manifest names** are deleted.

Blocking S3/Lance work; the cron route runs :func:`run_sweep` in the threadpool.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from time import perf_counter
from typing import Any, Literal

import lance
import pyarrow.fs as pafs
from botocore.exceptions import BotoCoreError, ClientError
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, Field

from maintenance.core.config import MaintenanceSettings, get_settings, shared_lance_session
from maintenance.core.lineage_emit import MaintenanceEmitter
from maintenance.core.metrics import (
    record_base_fragments_holding_deleted_rows,
    record_dataset_swept,
    record_failed,
    record_mixed_file_versions,
    record_reclaimed,
    record_refused,
    record_run,
    record_run_started,
    record_table_parked,
    record_trashed_skipped,
    record_unrecorded_bases,
)
from maintenance.services import base_reclaim, credentials, purge
from maintenance.services.discovery import discover_datasets
from maintenance.services.due import refresh_due, table_ids
from maintenance.services.expiry import DatasetResult, DueVerdict, ExpiryPolicy, ExpiryTarget, count_reclaimable, expire_versions, summarize_refusals
from maintenance.services.naming import PolicyOwners, TableIds, policy_owners, read_uuids, table_uuid, under_a_declared_root
from maintenance.services.plan import DatasetPlan, PlanContext, buckets_to_sweep, cadence_skip, load_policies, resolve_plan
from service_kit.governed import fga
from service_kit.governed.audit import AuditAction, AuditOutcome, audit
from service_kit.lakehouse import auto_cleanup, base_refs, base_registry, data_directories, location_claims, maintenance_policies, trash
from service_kit.lakehouse.features import flags_from_open_error, manifest_base_path_refs, manifest_feature_flags, mixes_data_file_versions
from service_kit.lakehouse.history_fences import TableHistory
from service_kit.lakehouse.lance_session import affordable_cache_bytes
from service_kit.lakehouse.maintenance_due import CloneList, DueRecord, delete_due, list_due, write_clone_list
from service_kit.lakehouse.objectfs import s3_filesystem
from service_kit.openlineage import DatasetUuids


log = logging.getLogger(__name__)


#: Per-tick cap on FAIL-event publishes. The over-cap set is SHUFFLED before the cut: discovery order is deterministic,
#: so a fixed head-slice would re-drop the SAME datasets every tick. The cap applies to the FAIL lane only: a FAIL
#: condition persists and converges on its deterministic run id next tick, while a COMPLETE reports work that happened
#: ONCE, so the COMPLETE lane is bounded by concurrency instead of by count.
_MAX_FAIL_EMITS_PER_TICK = 25

#: How many publishes may be in flight at once, in EITHER lane: a whole-estate gather is a fan-out of HTTP publishes at
#: one sidecar.
_MAX_CONCURRENT_EMITS = 25

tracer = trace.get_tracer(__name__)


def _s3fs(settings: MaintenanceSettings) -> pafs.S3FileSystem:
    """A pyarrow S3 filesystem over the store endpoint — used only to LIST the buckets."""
    return s3_filesystem(settings.storage_options())


def _discover_all(fs: pafs.FileSystem, buckets: list[str], *, max_depth: int) -> list[str]:
    """Every dataset URI across every swept bucket, reporting the prefixes the walk could not reach.

    A bucket that does not exist (or is unreadable) is skipped, not fatal: one missing tenant bucket must not stop the
    sweep for everyone else. A truncated prefix is logged, so a tick that never walked a prefix is distinguishable from
    one that walked everything.
    """
    uris: list[str] = []
    truncated: list[str] = []
    for bucket in buckets:
        try:
            found = discover_datasets(fs, bucket, max_depth=max_depth)
        except Exception as exc:  # noqa: BLE001 — one unreadable bucket must not stop the whole sweep
            log.warning("compaction_bucket_skipped", extra={"bucket": bucket, "error": str(exc)})
            continue
        log.info("compaction_bucket_discovered", extra={"bucket": bucket, "datasets": len(found.uris), "truncated": len(found.truncated)})
        uris.extend(found.uris)
        truncated.extend(found.truncated)
    if truncated:
        log.warning("maintenance_discovery_truncated", extra={"prefixes": len(truncated), "examples": sorted(truncated)[:5], "max_depth": max_depth})
    return uris


def _trash_exclusions(settings: MaintenanceSettings, options: dict[str, str]) -> dict[str, str]:
    """``{decoded path: why}`` for every dataset in the trash — the datasets this tick may NOT touch.

    #75 trash expiry is REPORT ONLY in the sweep; #79's purge lives on the reconcile tick, whose drift report gates it.

    An unreadable trash index ABORTS the tick, the policy registry's rule: the same read decides which datasets this
    tick may reclaim versions on, and reading it as empty would expire datasets an owner was told are frozen and
    recoverable. EVERY record counts, not just the expired ones: ``undrop`` does not consult the clock, so a passed
    deadline is not permission to touch the bytes.
    """
    try:
        trash_records = trash.list_all(settings.resolved_control_root, options)
    except Exception:
        log.error("compaction_trash_index_unreadable_tick_aborted")
        raise
    if due := purge.due_from(trash_records):
        log.info("trash_expiry_due_report_only", extra={"count": len(due), "ids": [str(r.get("id")) for r in due][:20]})
    return trashed_locations(trash_records)


def trashed_locations(trash_records: list[dict[str, Any]]) -> dict[str, str]:
    """``{decoded path: why}`` for every trash record naming bytes, each keyed as :func:`base_refs.decoded_path` reads it.

    ONE LOCATION, TWO SPELLINGS ([[LH-318]]). A record holds the location `describe_table` answered, which
    percent-encodes the table's name (``…$r%C3%A4ksm%C3%B6rg%C3%A5s``, ``…$growth%25``), while the store keeps, and
    discovery lists, the directory that spelling opens (``…$räksmörgås``, ``…$growth%``); measured on pylance 13.0.0,
    `dir` backend, over moto and a local root. Keyed as spelled, a recoverably dropped table with such a name would
    match no discovered dataset, and the sweep would expire the versions an undrop restores.

    A trashed NAMESPACE record and a declared-only table both carry `location=""`, and the empty path is a prefix of
    everything, so a record naming no location excludes nothing.
    """
    return {
        base_refs.decoded_path(str(record["location"])): f"in trash as {record.get('id') or '?'} (expires_at={record.get('expires_at') or '?'})"
        for record in trash_records
        if record.get("location")
    }


def why_in_trash(uri: str, trashed_by_path: dict[str, str]) -> str | None:
    """Why a trash record freezes ``uri``: the reason of the record naming its location or one holding it, else ``None``.

    CONTAINMENT, NOT EQUALITY: discovery lists each branch of a dataset at ``<dataset>/tree/<branch>``, so a record
    naming a table covers every dataset under it. A wrong refusal costs disk; a wrong permit writes to bytes an owner
    was told are frozen. One lookup per path segment, the nearest record first, so a whole-estate pass costs the same
    however many records the trash holds.

    ``uri`` is compared as the path it decodes to, the key :func:`trashed_locations` files a record under, so either
    spelling of a location answers alike: the decoded directory discovery lists, or the catalog's encoded one an
    event names.
    """
    segments = base_refs.decoded_path(uri).split("/")
    return next((why for end in range(len(segments), 0, -1) if (why := trashed_by_path.get("/".join(segments[:end]))) is not None), None)


def base_record_reader(settings: MaintenanceSettings, options: dict[str, str]) -> Callable[[str], base_registry.BaseRecord | None]:
    """How this service reads the catalog's base record for a table root ([[LH-279]]).

    The reader RAISES on a record it cannot read: answering "no record" would demote a recorded clone source to a
    finding and hand its bytes to reclamation.
    """
    registry = base_registry.BaseRegistry(control_root=settings.resolved_control_root, storage_options=options)
    return partial(base_registry.read_base_record, registry)


def _protected_roots(uris: list[str], options: dict[str, str], settings: MaintenanceSettings) -> base_refs.BaseRefs:
    """#128d/#114 THE PRE-PASS — over every discovered dataset in EVERY bucket, before one is touched.

    Whole-estate and first: a shallow clone in bucket B is the only thing that knows bucket A's dataset is referenced,
    and the SOURCE carries no flag and no ``base_paths`` of its own. ONLY A SANCTIONED BASE PROTECTS ([[LH-279]]): the
    referrer's own root, a configured external blob base, or one in the catalog's base record. Anything else a manifest
    declares is reported as ``maintenance_unrecorded_base`` and protects nothing. A base record that cannot be read
    ABORTS the tick. Uses the SERVICE's Lance session, so the operator's cache caps apply to the one loop that opens
    every dataset in the estate.
    """
    try:
        protected = base_refs.protected_roots(
            uris, options, configured=settings.external_blob_base_list, record_of=base_record_reader(settings, options), session=shared_lance_session()
        )
    except base_registry.UnreadableBaseRecordError:
        log.error("maintenance_base_record_unreadable_tick_aborted")
        raise
    _report_base_refs(protected)
    return protected


def _report_base_refs(protected: base_refs.BaseRefs) -> None:
    """Log what the pre-pass found and count its unrecorded bases. An unreadable dataset is named, never fatal."""
    if protected.protected or protected.pinned:
        log.info(
            "maintenance_protected_bases", extra={"count": len(protected.protected), "pinned": len(protected.pinned), "roots": sorted(protected.protected)[:20]}
        )
    record_unrecorded_bases(len(protected.findings))
    if protected.findings:
        bases = [{"referrer": finding.referrer, "base": finding.base, "relation": finding.relation.value} for finding in protected.findings[:20]]
        log.warning("maintenance_unrecorded_base", extra={"count": len(protected.findings), "bases": bases})
    if protected.unreadable:
        log.warning("maintenance_base_refs_incomplete", extra={"count": len(protected.unreadable), "datasets": [uri for uri, _ in protected.unreadable][:20]})


def _exclude_trashed(uris: list[str], trashed_by_path: dict[str, str]) -> tuple[list[str], list[DatasetResult]]:
    """Split the discovered URIs into the ones this tick may touch and a result per excluded dataset (F6(d)).

    A recoverable drop deregisters the table and moves no byte, so discovery still finds it, and each of its branches
    under it (:func:`why_in_trash`). AFTER :func:`_protected_roots`: a trashed shallow clone's base_paths are the only
    evidence protecting its LIVE source. COUNTED, never silently dropped, and each result names the record and its
    deadline.
    """
    skipped = [(uri, why) for uri in uris if (why := why_in_trash(uri, trashed_by_path)) is not None]
    record_trashed_skipped(len(skipped))
    if not skipped:
        return uris, []
    log.info("maintenance_trashed_excluded", extra={"count": len(skipped), "datasets": [uri for uri, _ in skipped][:20]})
    frozen = {uri for uri, _ in skipped}
    kept = [uri for uri in uris if uri not in frozen]
    return kept, [DatasetResult(uri=uri, trashed=reason) for uri, reason in skipped]


class SweepPlan(BaseModel):
    """What one tick decided before touching a dataset."""

    #: The datasets to expire and evaluate, shuffled.
    uris: list[str]
    #: The datasets decided without work: the trash exclusions.
    decided: list[DatasetResult] = Field(default_factory=list)
    protected: base_refs.BaseRefs = Field(default_factory=base_refs.BaseRefs)
    clones: CloneList
    table_ids: TableIds = Field(default_factory=TableIds)
    context: PlanContext


def _emit_zero_baselines() -> None:
    """Create every per-dataset series at 0 from the first tick: an estate with no datasets runs no dataset, and a
    counter nothing adds to is a series that does not exist — an alert on it reads "no data"."""
    record_reclaimed(versions_removed=0)
    record_refused(0)
    record_mixed_file_versions(0)
    record_unrecorded_bases(0)


def plan_sweep(settings: MaintenanceSettings) -> SweepPlan:
    """Decide what this tick covers, WITHOUT touching a dataset.

    ``record_run_started`` fires here, BEFORE discovery: a pass killed at dataset 400 of 900 is otherwise identical to a
    tick that never arrived, and started-minus-completed is the lost-pass count.
    """
    record_run_started()
    _emit_zero_baselines()
    options = settings.storage_options()
    now = datetime.now(UTC)
    policies = load_policies(settings, options)
    trashed_by_path = _trash_exclusions(settings, options)
    buckets = buckets_to_sweep(settings, options)
    uris = _discover_all(_s3fs(settings), buckets, max_depth=settings.discovery_max_depth)
    protected = _protected_roots(uris, options, settings)
    # A declared platform root is opened by explicit URI and named by no `__manifest` row, so it is no unnamed dataset
    # to refuse every tick; it is still read above, since a clone resolving its files there protects them.
    uris = [uri for uri in uris if not under_a_declared_root(uri, settings.declared_platform_root_list)]
    uris, decided = _exclude_trashed(uris, trashed_by_path)
    # Discovery order is deterministic, so a pass that consistently dies at dataset N would never reach what follows.
    random.shuffle(uris)
    return SweepPlan(
        uris=uris,
        decided=decided,
        protected=protected,
        clones=CloneList.taken(protected, at=now),
        table_ids=table_ids({f"s3://{bucket}" for bucket in buckets}, options),
        context=PlanContext(policies=policies, now=now, older_than=timedelta(days=settings.older_than_days)),
    )


class WriteProbe(BaseModel):
    """What one pre-vend open of a dataset tells us: whether expiry could write, and the manifest facts it reports."""

    may_write: bool
    data_storage_version: str | None = None
    mixed_data_file_versions: bool = False


def _probe_before_vending(
    uri: str, options: dict[str, str], *, policy: ExpiryPolicy, directories: list[base_registry.RecordedBase], now: datetime
) -> WriteProbe:
    """Could expiry write anything at all on this dataset?

    THE COST IT REMOVES, measured 2026-09-11: the store mints one STS identity record per vend and prunes none — 280 per
    minute on this estate — and the previous store became unable to restart at 107,485 of them. Expiry writes only
    when commit-path cleanup is armed (the disarm commits), when the policy's threshold of reclaimable versions is met,
    counted by the same rule the pass itself applies, or when the table's data directories hold a file the pass would
    delete ([[LH-286]]), found by the rule the pass applies again under its vend (`base_reclaim.unreferenced`).

    CONSERVATIVE BY CONSTRUCTION: an unreadable dataset answers `True`, because skipping a dataset that WOULD have been
    maintained is the failure this service exists to prevent while a spare credential is only a cost. Logged, never
    silent: the conservative answer otherwise hides a real fault as an extra credential.
    """
    try:
        ds = lance.dataset(uri, storage_options=options, session=shared_lance_session())
        armed = bool(auto_cleanup.armed_keys(ds.config()))
        reclaimable = policy.cleanup_enabled and count_reclaimable(ds, uri, policy) >= policy.min_reclaimable
        stranded = policy.cleanup_enabled and bool(directories) and bool(base_reclaim.unreferenced(ds, directories, options, now=now))
        return WriteProbe(
            may_write=armed or reclaimable or stranded,
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


def _on_vend_denied(result: DatasetResult, exc: Exception, table_id: str | None) -> None:
    result.park(str(exc), gate="vend_denied", table_id=table_id)


def _on_table_not_governed(result: DatasetResult, exc: Exception, table_id: str | None) -> None:
    result.park(str(exc), gate="table_not_governed", table_id=table_id)


def _on_vend_undecided(result: DatasetResult, exc: Exception, _table_id: str | None) -> None:
    result.error, result.error_type = f"maintain: {exc}", type(exc).__name__


#: How each answer of the vend door that stops the dataset is recorded, and the line it logs. A refusal and a park are
#: not failures: nothing broke, a grant is missing or a stamp is wrong. Only an undecided door is a failure, retried
#: next tick. Every one of them stops the dataset: anything done after it would be signed by the ambient key.
_VEND_ANSWERS: dict[type[Exception], tuple[str, Callable[[DatasetResult, Exception, str | None], None]]] = {
    credentials.GovernedElsewhere: ("maintenance_governed_elsewhere", _on_governed_elsewhere),
    credentials.MaintenanceUnauthenticated: ("maintenance_unauthenticated", _on_unauthenticated),
    credentials.MaintenanceDenied: ("maintenance_vend_denied", _on_vend_denied),
    credentials.TableNotGoverned: ("maintenance_table_not_governed", _on_table_not_governed),
    credentials.VendUndecided: ("maintenance_vend_undecided", _on_vend_undecided),
}


def _stopped_by_vend(probe: WriteProbe, target: ExpiryTarget, exc: Exception) -> DatasetResult:
    """The result of a dataset the vend door's answer stopped, carrying the census the probe already read."""
    event, record = _VEND_ANSWERS[type(exc)]
    log.warning(event, extra={"uri": target.uri, "table_id": target.table_id, "reason": str(exc)})
    stopped = DatasetResult(uri=target.uri, data_storage_version=probe.data_storage_version, mixed_data_file_versions=probe.mixed_data_file_versions)
    record(stopped, exc, target.table_id)
    return stopped


def _data_directories(uri: str, *, sweep: SweepPlan, settings: MaintenanceSettings, options: dict[str, str]) -> list[base_registry.RecordedBase]:
    """The data directories the table at ``uri`` holds, as its base record names them ([[LH-286]]).

    None for a branch, whose files are its table's, and none for a table whose manifest declares no plain base outside
    its own root: the record is read only for a table that could hold a directory. A record that cannot be read names
    none this tick, so the pass reclaims nothing on the base and expires the table's versions as usual; the next tick
    reads it again. Only a directory the record names and whose claim the table holds is ever listed, so neither a base a
    writer planted in the manifest nor one another table holds is reclaimed.
    """
    if (table := sweep.table_ids.of(uri)) is None:
        return []
    try:
        declared = manifest_base_path_refs(lance.dataset(uri, storage_options=options, session=shared_lance_session()))
    except Exception as exc:  # noqa: BLE001 — the pass opens the dataset again and reports its own failure
        log.debug("maintenance_data_directories_unopened", extra={"uri": uri, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return []
    if not any(not ref.is_dataset_root and not base_refs.location_in_store(uri, ref.path) for ref in declared):
        return []
    try:
        recorded = data_directories.recorded_directories(base_record_reader(settings, options)(uri))
        held = [directory for directory in recorded if data_directories.claimed_by(_claims(settings), directory, table)]
    except (base_registry.UnreadableBaseRecordError, ClientError, BotoCoreError, OSError, ValueError) as exc:
        log.warning("maintenance_base_record_unreadable", extra={"uri": uri, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return []
    if unheld := [directory.path for directory in recorded if directory not in held]:
        log.warning("maintenance_data_directory_not_held", extra={"uri": uri, "table": table, "directories": unheld})
    return held


def _expire(uri: str, named: NamedDataset, plan: DatasetPlan, *, sweep: SweepPlan, settings: MaintenanceSettings) -> DatasetResult:
    """One dataset's expiry, signed by a vend scoped to the table ``__manifest`` names it as, where expiry may write.

    One name feeds the vend and the result, so the table a credential is scoped to and the table events name cannot
    disagree. The pass holds that table's history fence, keyed by its uuid, while it deletes ([[LH-308]]), so a pass over
    a branch holds the same fence as a pass over its table.
    """
    table = named.table
    options = settings.storage_options()
    policy = plan.expiry_policy(min_reclaimable=settings.min_versions_to_expire, staged_grace=timedelta(seconds=settings.staged_manifest_grace_seconds))
    directories = _data_directories(uri, sweep=sweep, settings=settings, options=options)
    probe = _probe_before_vending(uri, options, policy=policy, directories=directories, now=sweep.context.now)
    history = TableHistory(control_root=settings.resolved_control_root, storage_options=options, table_uuid=named.owners.table_uuid)
    target = ExpiryTarget(
        uri=uri, storage_options=options, protected=sweep.protected, table_id=table, data_directories=directories, now=sweep.context.now, history=history
    )
    if probe.may_write:
        try:
            target.storage_options = credentials.write_options_for(uri, settings, table_id=table, fallback=options)
        except (credentials.MaintenanceDenied, credentials.TableNotGoverned, credentials.VendUndecided) as exc:
            return _stopped_by_vend(probe, target, exc)
    return expire_versions(target, policy)


def _expire_in_span(uri: str, named: NamedDataset, plan: DatasetPlan, *, sweep: SweepPlan, settings: MaintenanceSettings) -> DatasetResult:
    """:func:`_expire` inside its own span, timed around the work: a failure and a refusal are both on the span.

    A pass its lineage event will name carries the uuids of the table and its namespace, read only for such a pass.
    """
    with tracer.start_as_current_span("maintenance.expire_versions") as span:
        span.set_attribute("lance.maintenance.dataset_uri", uri)
        started = perf_counter()
        result = _expire(uri, named, plan, sweep=sweep, settings=settings)
        if _lane_of(result) is not None:
            result.uuids = read_uuids(sweep.table_ids, named.table, claims=_claims(settings))
        result.duration_seconds = round(perf_counter() - started, 3)
        span.set_attribute("lance.maintenance.duration_seconds", result.duration_seconds)
        if result.refused is not None:
            span.set_attribute("lance.maintenance.refused", result.refused)
        if result.error is not None:
            span.set_status(StatusCode.ERROR, result.error)
            span.set_attribute("error.type", result.error_type or "Unknown")
        return result


def _claims(settings: MaintenanceSettings) -> location_claims.ClaimStore:
    return location_claims.ClaimStore(control_root=settings.resolved_control_root, storage_options=settings.storage_options())


class NamedDataset(BaseModel):
    """The table a dataset belongs to, as ``__manifest`` names it, and the uuids whose records may govern it."""

    table: str
    owners: PolicyOwners


def name_dataset(uri: str, *, sweep: SweepPlan, settings: MaintenanceSettings) -> NamedDataset | DatasetResult:
    """The table ``uri`` belongs to with its policy owners, or the refusal of a dataset maintenance cannot identify."""
    table = sweep.table_ids.table_of(uri)
    if table is None:
        return _unnamed(uri, settings=settings)
    owners = policy_owners(sweep.table_ids, table, claims=_claims(settings))
    if owners is None:
        return _unidentified(uri, table, settings=settings)
    return NamedDataset(table=table, owners=owners)


def _evaluate_due(uri: str, named: NamedDataset, plan: DatasetPlan, *, sweep: SweepPlan, settings: MaintenanceSettings) -> DueVerdict:
    """Write or delete the due record of the table this dataset is; ``unnamed`` for a branch, which has none of its own."""
    if sweep.table_ids.of(uri) is None:
        return "unnamed"
    question = plan.due_question(table=named.table, uuid=named.owners.table_uuid, location=uri, clones=sweep.clones.clones_of(uri))
    return refresh_due(question, settings, now=sweep.context.now)


def _plan_for(uri: str, named: NamedDataset, *, sweep: SweepPlan, settings: MaintenanceSettings) -> DatasetPlan:
    """The dataset's plan, with the policy cadence applied."""
    plan = resolve_plan(uri, context=sweep.context, owners=named.owners)
    if plan.policy is not None and (skipped := cadence_skip(plan.policy, settings=settings, uri=uri, now=sweep.context.now)):
        plan.skipped = skipped
    return plan


def maintain_dataset(uri: str, *, sweep: SweepPlan, settings: MaintenanceSettings) -> DatasetResult:
    """One dataset's sweep pass: expiry, then the due evaluation, then its cadence stamp and counters."""
    record_dataset_swept()
    named = name_dataset(uri, sweep=sweep, settings=settings)
    if isinstance(named, DatasetResult):
        return named
    plan = _plan_for(uri, named, sweep=sweep, settings=settings)
    if plan.skipped is not None:
        return _skipped(uri, plan, settings=settings)
    result = _expire_in_span(uri, named, plan, sweep=sweep, settings=settings)
    result.due = _evaluate_due(uri, named, plan, sweep=sweep, settings=settings)
    _finish(result, plan=plan, now=sweep.context.now, settings=settings)
    return result


def expire_dataset(uri: str, *, sweep: SweepPlan, settings: MaintenanceSettings) -> DatasetResult:
    """One dataset's expiry, with the cadence, stamp and counters a sweep pass carries: what the event lane runs after
    a commit, ``sweep`` holding that one table."""
    named = name_dataset(uri, sweep=sweep, settings=settings)
    if isinstance(named, DatasetResult):
        return named
    plan = _plan_for(uri, named, sweep=sweep, settings=settings)
    if plan.skipped is not None:
        return _skipped(uri, plan, settings=settings)
    result = _expire_in_span(uri, named, plan, sweep=sweep, settings=settings)
    _finish(result, plan=plan, now=sweep.context.now, settings=settings)
    return result


def _unnamed(uri: str, *, settings: MaintenanceSettings) -> DatasetResult:
    """A dataset no ``__manifest`` row names, reported by its location and never opened, expired or vended ([[LH-520]]).

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


def _skipped(uri: str, plan: DatasetPlan, *, settings: MaintenanceSettings) -> DatasetResult:
    skipped = DatasetResult(uri=uri, skipped=plan.skipped)
    _record_dataset_outcome(skipped, subject=settings.catalog_service_identity)
    return skipped


def _finish(result: DatasetResult, *, plan: DatasetPlan, now: datetime, settings: MaintenanceSettings) -> None:
    _stamp_cadence(result, plan=plan, settings=settings, now=now)
    _record_dataset_metrics(result)
    _record_dataset_outcome(result, subject=settings.catalog_service_identity)


def _stamp_cadence(result: DatasetResult, *, plan: DatasetPlan, settings: MaintenanceSettings, now: datetime) -> None:
    """Record this dataset's ``last_maintained_at`` — only after a pass that could actually DO something.

    A refusal carries ``error=None``, so without ``refused is None`` a dataset the sweep can never maintain would read
    as a transient ``policy_interval`` skip for the whole interval rather than a standing refusal.
    """
    policy = plan.policy
    if policy is None or not policy.get("compact_interval_hours") or result.error is not None or result.refused is not None or result.skipped is not None:
        return
    try:
        maintenance_policies.write_state(settings.resolved_policy_root, settings.storage_options(), policy, result.uri, now.isoformat())
    except Exception as exc:  # noqa: BLE001 — a lost stamp re-paces one dataset; it must not fail the tick
        log.warning("compaction_policy_stamp_failed", extra={"policy": policy.get("id"), "uri": result.uri, "error": str(exc)})


def _record_dataset_metrics(result: DatasetResult) -> None:
    """The per-dataset counters, keyed by the STABLE error class and the refusing gate, never a message or a URI."""
    # A base file is a byte a superseded version stranded, so the bytes the sweep frees count both ([[LH-286]]).
    record_reclaimed(versions_removed=result.old_versions_removed, bytes_removed=result.bytes_removed + result.base_bytes_removed)
    record_base_fragments_holding_deleted_rows(result.base_fragments_holding_deleted_rows)
    record_refused(1 if result.refused else 0, result.refused_by)
    _record_parked(result)
    record_mixed_file_versions(1 if result.mixed_data_file_versions else 0)
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
    """One structured line per dataset per tick, the uneventful ones included: a dataset the sweep looked at and left
    alone is the answer to a real question. The fields ride `extra=`, so they land in GreptimeDB as `log_attributes`:

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
            "trashed": result.trashed,
            "error": result.error,
            "error_type": result.error_type,
            "data_storage_version": result.data_storage_version,
            "mixed_data_file_versions": result.mixed_data_file_versions,
        },
    )
    audit_material_work(result, subject=subject)


def execute_within_budget[T, R](
    items: Iterable[T],
    *,
    run: Callable[[T], R],
    budget_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> Iterator[R]:
    """Run each item, stopping BETWEEN items once ``budget_seconds`` is spent ([[LH-101]]).

    The cut is between items, never inside one: a reclaim interrupted half-way is worse than one not started. Zero is
    unlimited and the default. Exhaustion logs the executed and remaining counts, so a truncated tick says what it did
    not reach.
    """
    if budget_seconds <= 0:
        yield from (run(item) for item in items)
        return
    remaining = list(items)
    started = monotonic()
    executed = 0
    while remaining:
        if monotonic() - started >= budget_seconds:
            log.warning("sweep_budget_exhausted", extra={"executed": executed, "remaining": len(remaining), "budget_seconds": budget_seconds})
            return
        yield run(remaining.pop(0))
        executed += 1


def report_bucket_coverage(*, planned: Iterable[str], maintained: Iterable[str]) -> None:
    """Log what the tick MAINTAINED per bucket, beside what discovery reports it found ([[LH-101]]).

    A bucket with nothing maintained still reports: absence reads as "no such bucket", and a truncated tick starves
    buckets PARTIALLY because the dataset shuffle breaks bucket ordering.
    """
    counts: dict[str, list[int]] = {}
    for uri in planned:
        counts.setdefault(_bucket_of(uri), [0, 0])[0] += 1
    for uri in maintained:
        if (entry := counts.get(_bucket_of(uri))) is not None:
            entry[1] += 1
    for bucket, (planned_n, maintained_n) in sorted(counts.items()):
        log.info("compaction_bucket_maintained", extra={"bucket": bucket, "planned": planned_n, "maintained": maintained_n})


def _bucket_of(uri: str) -> str:
    """The bucket an s3 URI names; the whole string for anything else, so a local path still groups."""
    return uri.removeprefix("s3://").split("/", 1)[0] if uri.startswith("s3://") else uri


def clear_stale_due(settings: MaintenanceSettings, *, named: TableIds) -> int:
    """Delete the due record of every table no ``__manifest`` names any more; how many were deleted.

    A record is a table's while a row names its location and that table holds the record's uuid: a table registered at
    the location since holds another. Skipped when a manifest could not be read, since a table missing from an
    incomplete read may exist; a record whose table's uuid cannot be read is kept.
    """
    if not named.complete:
        log.warning("maintenance_due_clear_skipped_incomplete_manifests")
        return 0
    options = settings.storage_options()
    stale = [record for record in list_due(settings.resolved_control_root, options) if _is_stale(record, named, settings)]
    for record in stale:
        delete_due(settings.resolved_control_root, options, record.uuid)
    if stale:
        log.info("maintenance_due_cleared_unnamed", extra={"count": len(stale), "tables": [record.table for record in stale[:20]]})
    return len(stale)


def _is_stale(record: DueRecord, named: TableIds, settings: MaintenanceSettings) -> bool:
    table = named.of(record.location)
    if table is None:
        return True
    try:
        return table_uuid(named, table, claims=_claims(settings)) != record.uuid
    except Exception as exc:  # noqa: BLE001 — an unread claim keeps the record; the next tick reads it again
        log.warning("maintenance_due_owner_unread", extra={"table": table, "error": f"{type(exc).__name__}: {exc}"})
        return False


def run_sweep(settings: MaintenanceSettings) -> list[DatasetResult]:
    """One tick: plan, write the clone list, maintain each dataset within the budget, clear the stale due records.

    The clone list is written BEFORE any due record: the catalog's commit judge reads it to refuse a compaction that
    would break a clone, so a record must never be newer than the list it is judged against. A write that fails aborts
    the tick before a record is written.
    """
    sweep = plan_sweep(settings)
    write_clone_list(settings.resolved_control_root, settings.storage_options(), sweep.clones)
    results = list(sweep.decided)
    results.extend(
        execute_within_budget(sweep.uris, run=partial(maintain_dataset, sweep=sweep, settings=settings), budget_seconds=settings.sweep_budget_seconds)
    )
    clear_stale_due(settings, named=sweep.table_ids)
    report_bucket_coverage(planned=sweep.uris, maintained=[result.uri for result in results])
    # The completion half of the `record_run_started` pair: started minus completed is the lost-pass count.
    record_run()
    return results


def _did_material_work(result: DatasetResult) -> bool:
    """Did the pass reclaim any version, or any file in the table's data directories? Only then is a maintenance run
    recorded: a cron re-sweeping every dataset would otherwise flood the lineage graph and the compliance trail with
    no-op runs."""
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


async def _emit_complete(emitter: MaintenanceEmitter, *, table_id: str, namespace: str, uuids: DatasetUuids) -> None:
    """The COMPLETE publish, a coroutine function so the attribute lookup happens INSIDE :func:`_publish_all`'s guard."""
    await emitter.emit_maintenance(table_id=table_id, namespace=namespace, uuids=uuids)


async def _emit_failed(emitter: MaintenanceEmitter, *, table_id: str, namespace: str, error: str, uuids: DatasetUuids) -> None:
    """The FAIL publish — same late-binding rule as :func:`_emit_complete`."""
    await emitter.emit_maintenance_failed(table_id=table_id, namespace=namespace, error=error, uuids=uuids)


async def _publish_all(kind: str, jobs: list[tuple[str, Callable[[], Awaitable[None]]]]) -> None:
    """Run every publish in ``jobs`` concurrently, at most ``_MAX_CONCURRENT_EMITS`` in flight, raise-proof per publish."""
    if not jobs:
        return
    limiter = asyncio.Semaphore(_MAX_CONCURRENT_EMITS)

    async def _one(factory: Callable[[], Awaitable[None]]) -> None:
        async with limiter:
            await factory()

    try:
        outcomes = await asyncio.gather(*(_one(factory) for _table_id, factory in jobs), return_exceptions=True)
    except Exception as exc:
        log.warning(f"maintenance_{kind}_emit_error", extra={"error": str(exc)})
        return
    for (table_id, _factory), outcome in zip(jobs, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            log.warning(f"maintenance_{kind}_emit_error", extra={"table": table_id, "error": str(outcome)})


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


class _Emit(BaseModel):
    """One dataset's lineage event: the table it names, that table's namespace, their uuids, and a failure's error."""

    table_id: str
    namespace: str
    uuids: DatasetUuids
    error: str = ""


def _emit_selection(results: list[DatasetResult]) -> tuple[list[_Emit], list[_Emit]]:
    """``(complete, failed)``: one :class:`_Emit` per dataset that reclaimed versions, and one per ``maintain:`` failure."""
    complete: list[_Emit] = []
    failed: list[_Emit] = []
    for result in results:
        lane = _lane_of(result)
        if lane is None or result.table_id is None:
            continue
        emit = _Emit(
            table_id=result.table_id,
            namespace=fga.parent_namespace_id(result.table_id) or "",
            uuids=result.uuids,
            error=result.error or "",
        )
        (failed if lane == "fail" else complete).append(emit)
    return complete, failed


async def emit_sweep_lineage(emitter: MaintenanceEmitter, results: list[DatasetResult]) -> None:
    """Emit a best-effort maintenance event per swept dataset: COMPLETE for reclaimed versions, FAIL for a terminal
    per-dataset failure (#7b + §4 failure visibility). Nothing here raises into the sweep.

    The parent namespace is derived via :func:`service_kit.governed.fga.parent_namespace_id`, so events land on the SAME
    ``(:Dataset)`` the catalog created. Only the FAIL lane is capped at ``_MAX_FAIL_EMITS_PER_TICK``.
    """
    complete, failed = _emit_selection(results)
    if len(failed) > _MAX_FAIL_EMITS_PER_TICK:
        log.warning("maintenance_fail_emits_capped", extra={"failing": len(failed), "emitted": _MAX_FAIL_EMITS_PER_TICK})
        random.shuffle(failed)
        failed = failed[:_MAX_FAIL_EMITS_PER_TICK]
    await _publish_all(
        "complete", [(e.table_id, partial(_emit_complete, emitter, table_id=e.table_id, namespace=e.namespace, uuids=e.uuids)) for e in complete]
    )
    await _publish_all(
        "fail", [(e.table_id, partial(_emit_failed, emitter, table_id=e.table_id, namespace=e.namespace, error=e.error, uuids=e.uuids)) for e in failed]
    )


def summarize(results: list[DatasetResult]) -> dict[str, Any]:
    """Aggregate one sweep's results into the cron response. Failures keep their MESSAGE: a cron sweep has no human
    watching, so the *why* is the only debugging signal. A refusal and a trash exclusion are their own lines, never
    folded into ``errors`` or ``skipped``: ``SUPPORTED`` is a whitelist, so a pylance upgrade that adds a legitimate flag
    shows up only here and in ``compaction.datasets.refused``."""
    return {
        "datasets": len(results),
        "skipped": sum(1 for r in results if r.skipped),
        "refused": sum(1 for r in results if r.refused),
        "refusals": {r.uri: r.refused for r in results if r.refused},
        "refused_by": summarize_refusals(results),
        "trashed": sum(1 for r in results if r.trashed),
        "trashed_datasets": {r.uri: r.trashed for r in results if r.trashed},
        "versions_removed": sum(r.old_versions_removed for r in results),
        "bytes_removed": sum(r.bytes_removed for r in results),
        "auto_cleanup_disarmed": sum(1 for r in results if r.auto_cleanup_disarmed),
        "due": dict(Counter(r.due for r in results if r.due is not None)),
        "errors": {r.uri: r.error for r in results if r.error},
        **memory_readings(),
    }


def memory_readings() -> dict[str, int]:
    """The three numbers [[LH-183]] turns on: ``python_blocks`` tracking RSS means Python retention, flat while RSS
    climbs means native allocation. ONE SEAM because they are one measurement; a subset reports nothing."""
    return {**_session_occupancy(), "python_blocks": _python_heap_blocks(), "rss_bytes": _rss_bytes()}


def _rss_bytes() -> int:
    """Resident set size of THIS process, from ``/proc/self/status``; ``-1`` where procfs is absent.

    ``VmRSS`` and not ``ru_maxrss``, which is a HIGH-WATER mark and so cannot tell a tick that grew and released from
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
