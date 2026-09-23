"""The periodic storage->graph reconciliation route — a Dapr cron binding fires the back-fill sweep (B4).

A ``bindings.cron`` component POSTs to ``/<binding-name>`` on a schedule; OPTIONS is Dapr's binding-discovery
pre-flight. The sweep reconciles every dataset the graph knows against on-disk Lance and **back-fills** any
write whose lineage event was lost (the outbox gap) — the buildable half of the outbox problem, since a
stateless catalog over object storage has no DB to host a transactional outbox. Guarded by
``require_dapr_token`` so only the sidecar's cron may drive it. The schedule + binding name live in the
chart, not app code (no scheduler thread here). Blocking Lance/S3 reads run in the threadpool.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import PermissionDeniedError
from pydantic import BaseModel, Field, ValidationError

from lineage.api.dependencies import PublisherDep, RepositoryDep, SettingsDep
from lineage.api.fga_deps import enforce_bus_authz
from lineage.core.config import LineageSettings, declared_columns_map, storage_options
from lineage.core.metrics import record_provenance_gaps
from lineage.core.reconcile import (
    BACKFILLABLE_STATES,
    read_dangling_blob_columns,
    read_latest_write_age_hours,
    read_storage_schema,
    read_storage_version,
    read_storage_versions,
    read_version_operations,
    reconcile_all,
)
from lineage.models import RunEvent, author_sub_from_payload
from lineage.schemas import ReconcileState, ReconcileStatus
from service_kit import dapr_publish
from service_kit.governed import fga
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.lakehouse import outbox, outbox_metrics


log = logging.getLogger(__name__)


class SweepReport(BaseModel):
    """One cron tick's findings — the tick's response body and the shape its log line counts.

    A model rather than a hand-built ``dict[str, Any]``: the tick reports NINE independent finding classes
    plus two counters, and the response was assembled twice in one function body (once as a log ``extra``,
    once as the return) from literal keys that could drift apart silently.
    """

    checked: int = 0
    backfilled: list[str] = Field(default_factory=list)
    storage_loss: list[str] = Field(default_factory=list)
    #: Datasets carrying NO authorization tuple — not governed tables, so not data anyone lost. Its own
    #: field rather than a share of `storage_loss` for the reason the `graph_ahead` split already
    #: established here: an alarm that is mostly benign is one an operator learns to skim.
    ungoverned: list[str] = Field(default_factory=list)
    graph_ahead: list[str] = Field(default_factory=list)
    unreadable: dict[str, str | None] = Field(default_factory=dict)
    dangling_blobs: dict[str, list[str]] = Field(default_factory=dict)
    stale: list[str] = Field(default_factory=list)
    contract_violations: dict[str, list[str]] = Field(default_factory=dict)
    provenance_holes: dict[str, list[int]] = Field(default_factory=dict)
    #: Governed tables the graph holds NO dataset node for — the commit->stage gap on a FIRST write,
    #: which every other axis here is structurally blind to because they all start from the graph.
    #: Measured on the deployed estate 2026-09-19: 1,424 governed tables against 1,297 graph datasets,
    #: 127 of them with no node, and 0 graph datasets that were not governed.
    #:
    #: ``None`` IS NOT ``[]``, for the reason `record_provenance_gaps` states about the gauge: an empty
    #: list means the sweep ASKED and every governed table has a node — a clean bill of health — while
    #: ``None`` means FGA was off or its store unreadable and the question went unasked. Collapsing the
    #: two would report the moment the sweep lost its sight as the moment the estate became healthy.
    unknown_to_graph: list[str] | None = None
    outbox_drained: int = 0
    outbox_stranded: int = 0
    outbox_refused: int = 0
    #: Refusals whose verdict reached Postgres ([[LH-182]]). Beside `outbox_refused` because the pair is
    #: what makes the drain verifiable: equal means the loop closes, `0` against a non-zero refusal count
    #: means it does not, and `outbox_refused` alone cannot distinguish them.
    outbox_refusals_recorded: int = 0
    pruned_runs: int = 0
    pruned_events: int = 0


class DrainOutcome(BaseModel):
    """What one outbox drain did — THREE numbers, because any one alone reads as the opposite of the truth.

    ``drained`` alone says a tick succeeded while a specific event has been refused on every tick since
    the estate came up; ``stranded`` alone says a tick failed while it recovered everything else. The set
    is what distinguishes "the relay is working and one event needs a human" from "the relay is wedged",
    and those need different responses.

    ``refused`` IS THAT DISTINCTION MADE LOAD-BEARING rather than left to a reader. A governance refusal
    is not a failed tick: the event is well-formed, the graph's answer is deterministic, and no retry
    changes it. Counted as ``stranded`` it made the pair say both things at once — measured on the live
    estate, `drained=0 stranded=6` unchanged for 2.1 days while the relay was healthy (it drained
    `drained=1 stranded=0` the moment ingest's credential landed), so the numbers reported a wedged relay
    that did not exist.

    THE HANDLING IS UNCHANGED AND THE EVENT STAYS STAGED. This splits the REPORTING only. Retiring a
    permanently-refused event is not available to this service: moving it aside is a PutObject and the
    relay is denied that by policy (`test_the_lineage_plane_writes_nothing_it_does_not_own.py` pins
    `not (allowed & {"s3:PutObject", ...})`), and destroying the only durable copy of a committed write's
    provenance is the wrong answer to "you may not record this" in any case.
    """

    drained: int = 0
    stranded: int = 0
    refused: int = 0
    #: Refusals whose verdict reached Postgres, and whose object was therefore safe to drop.
    #:
    #: [[LH-182]] `refused` COUNTS REFUSALS HANDLED, NOT OBJECTS REMAINING, so it reads identically
    #: whether a tick retired seven staged events or re-refused the same seven for the hundredth time.
    #: Measured 2026-09-21 on the deployed estate, that is exactly what happened: the tick logged
    #: `refused=7` before the drain existed and `refused=7` after it, and no amount of log-reading
    #: could tell the two apart. A number that answers the same thing in opposite states is the shape
    #: `compaction_mode` already cost this estate once.
    #:
    #: It is the TERMINAL action that is worth counting: `recorded == refused` means the loop is
    #: closing, and `recorded == 0` with `refused > 0` means it is not.
    recorded: int = 0


def summarize_sweep(statuses: list[ReconcileStatus], *, governed: set[str] | None = None, graph: set[str] | None = None) -> SweepReport:
    """Partition one sweep's statuses into the tick's finding classes.

    Pure and public so the unit tier can drive a partition from a handful of statuses instead of standing
    up a whole sweep. ``outbox_drained`` / ``pruned_runs`` / ``pruned_events`` are not derived from
    statuses — the caller stamps them on.

    ``governed`` AND ``graph`` ARE THE TWO INPUTS NOT DERIVED FROM ``statuses``, and both have to be.
    A governed table the graph never recorded appears in no status, so no partition of statuses can
    find it — that is ``governed``. And a status is produced only for a dataset the sweep actually
    reconciled, while a dataset with no ``dataSource`` URI or a drop stamp is skipped though the graph
    knows it perfectly well — so ``{s.dataset for s in statuses}`` is the wrong second operand. Measured
    on the deployed estate 2026-09-19: differencing against the statuses reported 1,007 invisible
    tables where the graph's own listing gives 127.

    ``None`` on either side means the question was not asked (FGA off, no client, an unreadable store,
    or a caller that did not collect the enumeration) and must find NOTHING — an empty set would report
    every table in the estate as invisible at the moment the sweep lost the ability to ask.
    """
    return SweepReport(
        unknown_to_graph=sorted(governed - graph) if governed is not None and graph is not None else None,
        checked=len(statuses),
        backfilled=[s.dataset for s in statuses if s.status in BACKFILLABLE_STATES],
        storage_loss=[s.dataset for s in statuses if s.status is ReconcileState.MISSING_ON_STORAGE],
        ungoverned=[s.dataset for s in statuses if s.status is ReconcileState.UNGOVERNED],
        graph_ahead=[s.dataset for s in statuses if s.status is ReconcileState.GRAPH_AHEAD],
        unreadable={s.dataset: s.unreadable_reason for s in statuses if s.status is ReconcileState.UNREADABLE},
        dangling_blobs={s.dataset: s.dangling_blob_columns for s in statuses if s.dangling_blob_columns},
        stale=[s.dataset for s in statuses if s.stale],
        contract_violations={s.dataset: s.missing_declared_columns for s in statuses if s.missing_declared_columns},
        provenance_holes={s.dataset: s.versions_without_lineage for s in statuses if s.versions_without_lineage},
    )


def record_sweep(report: SweepReport) -> None:
    """Publish the tick's two provenance-completeness gauges — the metric half of :func:`log_sweep`.

    Named and beside it rather than inlined in the tick, because the tick's body is budgeted
    (`test_reconcile_sweep_shape.py` caps `_on_cron` at 45 lines) and because a WARN alone cannot page:
    `chart/alerting/rules.yml` evaluates series, not log bodies.

    THE TWO CLASSES ARE COUNTED DIFFERENTLY ON PURPOSE. `unknown_to_graph` counts TABLES — one per
    governed table with no node — while `versions_below_tip` counts VERSIONS across datasets, because a
    single dataset missing forty versions is forty lost writes, not one.
    """
    record_provenance_gaps(
        unknown_to_graph=len(report.unknown_to_graph) if report.unknown_to_graph is not None else None,
        versions_below_tip=sum(len(v) for v in report.provenance_holes.values()),
    )


def log_sweep(report: SweepReport) -> None:
    """Emit one WARN per non-empty finding class, then the tick's INFO summary.

    Every class here is a finding the sweep CANNOT auto-fix, which is why each gets its own line rather
    than a count buried in the summary:

    * ``graph_ahead`` — the dataset READ fine and sits at an OLDER version than the graph records; a
      drop-and-recreate leaves this, so it is usually benign and is reported apart from real loss.
    * ``storage_loss`` — the graph claims data on-disk Lance no longer has (a bad restore, a wipe). The
      data is gone; only a human can answer for it.
    * ``ungoverned`` — the dataset holds NO authorization tuple, so it is not a governed table at all.
      Reported apart from loss because the two demand different responses: loss is an incident a person
      answers for, while a table nobody can reach is residue or a lost grant. Measured 2026-09-11, all
      three datasets ``storage_loss`` named were this, so the line was 100% miscategorised.
    * ``unreadable`` — datasets this reader could not OPEN, reported on their own line and deliberately
      NOT counted as loss: "we could not read it" and "it is gone" demand opposite responses. Before
      they were separated, six live datasets carrying an unsupported manifest feature flag were reported
      as destroyed, in the same hour ``services/maintenance`` was reading their manifests. WARN rather
      than ERROR because the data is very likely fine; what is broken is our ability to see it, and the
      reason says which.
    * ``dangling_blobs`` — payloads gone from under a version-wise-healthy table; the bytes are lost.
    * ``stale`` — data stopped arriving inside the freshness budget; the fix is upstream.
    * ``contract_violations`` — a dataset's CURRENT schema lost a column a consumer declared, i.e. a
      write that bypassed the stage runner skipped the gate.
    * ``provenance_holes`` — versions on disk the graph held no ``WROTE`` edge for. The ONE class here
      the sweep does auto-fix, and it is still worth a line of its own: the recovered edge carries
      ``author='reconcile'`` and no inputs, so the version's ACTOR and DERIVATION are gone for good even
      though the fact of the write is restored. A hole that keeps reappearing on the same dataset names a
      producer that is not emitting, which is a defect upstream and invisible in the backfilled count.
    """
    if report.storage_loss:
        log.warning("lineage_reconcile_storage_loss", extra={"datasets": report.storage_loss, "count": len(report.storage_loss)})
    if report.ungoverned:
        # ITS OWN BODY, like `graph_ahead` beside it and for the same reason. A dataset here holds no
        # authorization tuple, so nobody — including whoever created it — can read, maintain, drop or
        # re-create it; its bytes being absent is not something a person can answer for. Measured
        # 2026-09-11, all three datasets the loss line named were this.
        log.warning("lineage_reconcile_ungoverned", extra={"datasets": report.ungoverned, "count": len(report.ungoverned)})
    if report.graph_ahead:
        # ITS OWN BODY, because the two findings differ in kind and an operator filters on the body. A
        # dataset here was READ successfully and sits at an older version than the graph — an e2e run
        # that drops and recreates a table leaves exactly this. Measured 2026-09-11, 29 of the 32 the
        # single `storage_loss` line reported were live readable tables, so the real loss it also carried
        # was the 9% no one could see.
        log.warning("lineage_reconcile_graph_ahead", extra={"datasets": report.graph_ahead, "count": len(report.graph_ahead)})
    if report.unreadable:
        log.warning("lineage_reconcile_unreadable", extra={"datasets": report.unreadable, "count": len(report.unreadable)})
    if report.dangling_blobs:
        log.warning("lineage_reconcile_dangling_blobs", extra={"datasets": report.dangling_blobs, "count": len(report.dangling_blobs)})
    if report.stale:
        log.warning("lineage_reconcile_stale", extra={"datasets": report.stale, "count": len(report.stale)})
    if report.contract_violations:
        log.warning("lineage_reconcile_contract_violation", extra={"datasets": report.contract_violations, "count": len(report.contract_violations)})
    if report.unknown_to_graph:
        # ITS OWN BODY, like every sibling. This is the only class naming a table the sweep has no node
        # for, so it is the only one an operator cannot chase from the graph — the name here is the
        # entire lead. NOT auto-fixed: a node invented for a table the graph never saw would assert a
        # write nobody observed, and the dataset has no `dataSource` URI to check it against.
        log.warning("lineage_reconcile_unknown_to_graph", extra={"tables": report.unknown_to_graph, "count": len(report.unknown_to_graph)})
    if report.provenance_holes:
        log.warning(
            "lineage_reconcile_provenance_holes",
            extra={
                "datasets": report.provenance_holes,
                "count": len(report.provenance_holes),
                "versions": sum(len(v) for v in report.provenance_holes.values()),
            },
        )
    log.info(
        "lineage_reconcile_sweep",
        extra={
            "checked": report.checked,
            "backfilled": len(report.backfilled),
            "storage_loss": len(report.storage_loss),
            "ungoverned": len(report.ungoverned),
            "graph_ahead": len(report.graph_ahead),
            "unreadable": len(report.unreadable),
            "dangling_blobs": len(report.dangling_blobs),
            "stale": len(report.stale),
            "contract_violations": len(report.contract_violations),
            "provenance_holes": sum(len(v) for v in report.provenance_holes.values()),
            "unknown_to_graph": len(report.unknown_to_graph) if report.unknown_to_graph is not None else None,
            "outbox_drained": report.outbox_drained,
            "outbox_stranded": report.outbox_stranded,
            "outbox_refused": report.outbox_refused,
            "outbox_refusals_recorded": report.outbox_refusals_recorded,
            "pruned_runs": report.pruned_runs,
            "pruned_events": report.pruned_events,
        },
    )


async def governed_tables(request: Request, settings: LineageSettings) -> set[str] | None:
    """The table ids carrying at least one authorization tuple, or ``None`` when the answer is unknown.

    ``None`` IS THE LOAD-BEARING CASE and the reason this returns an optional rather than a set. An
    empty set means "nothing in this estate is governed", which would classify every dataset UNGOVERNED
    and erase the loss axis entirely — at exactly the moment something is wrong. FGA off, no client
    wired, or a store that could not be enumerated are all "we did not ask", and a sweep that did not
    ask must classify on what it does know.

    ONE ENUMERATION PER SWEEP, not one check per dataset: OpenFGA refuses a Read whose ``tuple_key``
    carries an empty object id, so the per-object shape is the only one most callers find — and it
    costs N calls a tick. `fga.governed_objects` omits the filter instead and pages the store, measured
    at 51 pages / 5,027 tuples / 0.1 s for this whole estate.
    """
    if not settings.fga_enabled:
        return None
    client = getattr(request.app.state, "fga", None)
    if client is None:
        return None
    try:
        return await fga.governed_objects(client, object_type=settings.fga_object_type)
    except Exception as exc:  # noqa: BLE001 — an unreadable store must degrade the axis, never the sweep
        log.warning("lineage_reconcile_governed_set_unreadable", extra={"error": str(exc)})
        return None


async def _swept_report(request: Request, repository: RepositoryDep, settings: SettingsDep, opts: dict[str, str]) -> SweepReport:
    """Run one sweep and partition it — the two halves that must agree about the same two sets.

    Named rather than inlined in the tick for the reason `test_reconcile_sweep_shape.py` enforces, and
    because the three sets here are easy to pair up wrongly. The governed enumeration is read in BOTH
    directions (mark a graph dataset nobody governs; find a governed table the graph never recorded),
    and the graph's listing is what the second direction differences against — NOT the statuses, which
    omit every dataset the sweep skipped. Computing them in one place is what keeps the two directions
    from being asked of two different answers.
    """
    governed = await governed_tables(request, settings)
    graph: set[str] = set()
    statuses = await _sweep(repository, settings, opts, governed, graph)
    return summarize_sweep(statuses, governed=governed, graph=graph)


async def _sweep(
    repository: RepositoryDep, settings: SettingsDep, opts: dict[str, str], governed: set[str] | None = None, enumerated: set[str] | None = None
) -> list[ReconcileStatus]:
    """Reconcile every dataset against storage, back-filling any write whose lineage event was lost.

    The Lance reads all run in the threadpool so the object-store I/O never stalls the event loop.

    ``enumerated`` is passed straight through so the caller learns what the GRAPH holds, which is a
    superset of what produced a status — see :func:`reconcile_all`.
    """
    return await reconcile_all(
        repository,
        lambda uri: run_in_threadpool(read_storage_version, uri, opts),
        backfill=True,
        enumerated=enumerated,
        # Recover the per-version schema for a back-filled write too (#24) — pinned to the version
        # being back-filled so a mid-sweep write can't attach a later schema to the recovered edge.
        read_schema=lambda uri, ver: run_in_threadpool(read_storage_schema, uri, opts, ver),
        # Blob-pointer health (§9 P1 lifecycle) — the axis version comparison can't see: an
        # external payload deleted AFTER promotion changes no Lance version. Same shared probe
        # the quality gate runs; two 1-byte reads per blob column.
        read_dangling=lambda uri: run_in_threadpool(read_dangling_blob_columns, uri, opts),
        # Freshness (data-contract gap #2) — arrival cadence as an ASSERTED clause: age read from
        # the version manifests (storage truth), budget 0 (default) = axis off, zero extra reads.
        read_age=lambda uri: run_in_threadpool(read_latest_write_age_hours, uri, opts),
        # Provenance holes BELOW the tip — the axis the two-maxima version comparison is blind to. One
        # manifest-directory listing per dataset, the same one the freshness axis above already pays, and
        # it is what makes "a write's provenance survives it" true for a write that was later superseded.
        read_versions=lambda uri: run_in_threadpool(read_storage_versions, uri, opts),
        # Which of those holes are real. A compaction/index/config version commits with no lineage BY
        # DESIGN, so classifying is what keeps the finding worth reading; one transaction read per hole,
        # and a healthy dataset has none.
        read_operations=lambda uri, versions: run_in_threadpool(read_version_operations, uri, opts, versions),
        freshness_budget_hours=settings.freshness_budget_hours,
        # Declared-columns patrol (Batch 23): re-check the gate's column_declared assertion
        # estate-wide — only declared datasets pay the schema read.
        declared=declared_columns_map(settings),
        # WHICH TABLES ANYONE HOLDS A TUPLE ON. A dataset absent from this set is not a governed table,
        # so every axis above would be reasoning about something nobody can reach — and reporting its
        # missing bytes as loss sends an operator after data no person can answer for. `None` means the
        # question was not asked; see `governed_tables`.
        governed=governed,
    )


async def _prune_old_runs(repository: RepositoryDep, settings: SettingsDep) -> int:
    """Opt-in Run retention (§4) — drop graph runs older than the budget; 0 days (default) keeps everything.

    Runs while the caller still holds the single-flight lock, so two replicas never race the same delete.
    Isolated: a prune failure degrades to a warning, never 500s the tick — the sweep above already
    completed and its report must reach the log/response regardless.
    """
    if not settings.run_retention_days:
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=settings.run_retention_days)).isoformat()
    try:
        pruned = await repository.prune_runs(cutoff)
    except Exception as exc:
        log.warning("lineage_run_prune_failed", extra={"error": str(exc)})
        return 0
    if pruned:
        log.info("lineage_runs_pruned", extra={"pruned": pruned, "retention_days": settings.run_retention_days})
    # THE SECOND HALF, and retention does not converge without it: pruning runs leaves their datasets
    # behind, so the graph keeps a node per table any expired run ever touched and the reconcile keeps
    # probing them. Measured 2026-09-08 — 1,271 Dataset nodes, every one still edge-reachable.
    #
    # LOSING ITS LAST RUN IS NOT SUFFICIENT. `cypher._ORPHAN_DATASETS` also requires no `CREATED` edge,
    # and that edge comes from a `User` rather than a Run, so no run prune can ever remove it: measured
    # 2026-09-11, 1145 of 1247 Dataset nodes carry one and the orphan query matches 0. The call belongs
    # here — a run-less dataset is what retention leaves behind — but it reclaims nothing for a
    # user-created table, so accumulated residue needs a remedy that is not this one.
    #
    # ONLY AFTER a successful run prune, and contained the same way: an orphan-prune failure must not
    # lose the run-prune count the caller already earned, and neither may end the reconcile.
    try:
        orphans = await repository.prune_orphan_datasets()
    except Exception as exc:  # noqa: BLE001 — retention is best-effort; the sweep's report still lands
        log.warning("lineage_dataset_prune_failed", extra={"error": str(exc)})
        return pruned
    if orphans:
        log.info("lineage_orphan_datasets_pruned", extra={"pruned": orphans})
    # AND THE JOB BETWEEN THEM, which nothing reclaimed. Same containment as the two above: a failure
    # here must not lose the counts the caller already earned, and must not end the reconcile.
    try:
        stale_jobs = await repository.prune_orphan_jobs()
    except Exception as exc:  # noqa: BLE001 — retention is best-effort; the sweep's report still lands
        log.warning("lineage_job_prune_failed", extra={"error": str(exc)})
        return pruned
    if stale_jobs:
        log.info("lineage_orphan_jobs_pruned", extra={"pruned": stale_jobs})
    return pruned


async def _prune_old_events(repository: RepositoryDep, settings: SettingsDep) -> int:
    """Durable-feed retention — drop rows RECEIVED longer ago than the budget; 0 days keeps everything.

    Runs beside `_prune_old_runs`, under the same single-flight lock and with the same isolation: a
    retention failure degrades to a warning rather than 500ing a tick whose sweep already completed.

    IT IS HERE RATHER THAN ON THE INGEST PATH for two reasons that only this position satisfies: the
    feed's hottest path must not pay for a retention DELETE per event, and two replicas ingesting
    concurrently must not be able to race the same delete. One pass per tick under the lock has neither
    problem.
    """
    if not settings.events_retention_days:
        return 0
    try:
        pruned = await repository.prune_events(settings.events_retention_days)
    except Exception as exc:
        log.warning("lineage_event_prune_failed", extra={"error": str(exc)})
        return 0
    if pruned:
        log.info("lineage_events_pruned", extra={"pruned": pruned, "retention_days": settings.events_retention_days})
    return pruned


async def _on_cron(
    request: Request,
    repository: RepositoryDep,
    settings: SettingsDep,
    publisher: PublisherDep,
    _: Annotated[None, Depends(require_dapr_token)],
) -> dict[str, Any]:
    """One reconciliation sweep, triggered by a Dapr cron tick: back-fill any dropped Lance writes.

    Three steps run under one lock — the storage sweep (:func:`_sweep`), the outbox drain
    (:func:`_drain_outbox`, the FULL-event recovery the version back-fill cannot do) and run retention
    (:func:`_prune_old_runs`) — then :func:`summarize_sweep` turns the statuses into the tick's report.
    Best-effort per the cron contract: the drain and the prune each degrade to a warning, because the
    back-fill has already committed and its report must reach the caller either way.

    Single-flight: the cron fires on EVERY lineage replica independently, so the sweep runs under a
    cluster-wide advisory lock. A tick that finds a sweep already in progress skips (the next tick retries)
    rather than double-driving the same back-fill.
    """
    async with repository.reconcile_lock() as acquired:
        if not acquired:
            log.info("lineage_reconcile_skipped_locked")
            return {"skipped": True, "reason": "another reconcile sweep is in progress"}
        opts = storage_options(settings)
        # BEFORE the sweep, and `_drain_outbox` carries why. Both reasons — cost and fidelity — are
        # properties of the drain relative to the sweep, so they live with the function that has them.
        outcome = DrainOutcome()
        if settings.outbox_uri:
            try:
                outcome = await _drain_outbox(request, repository, settings, opts, publisher)
            except Exception as exc:
                log.warning("lineage_outbox_drain_failed", extra={"error": str(exc)})
        # ONE enumeration, read in BOTH directions. `_sweep` uses it to mark a graph dataset nobody
        # governs; `summarize_sweep` uses it to find a governed table the graph never recorded. Computed
        # here rather than inline so the two directions cannot be asked of two different answers.
        report = await _swept_report(request, repository, settings, opts)
        report.outbox_drained, report.outbox_stranded, report.outbox_refused = outcome.drained, outcome.stranded, outcome.refused
        report.outbox_refusals_recorded = outcome.recorded
        report.pruned_runs = await _prune_old_runs(repository, settings)
        report.pruned_events = await _prune_old_events(repository, settings)
    record_sweep(report)
    log_sweep(report)
    return report.model_dump()


async def _drain_outbox(
    request: Request, repository: RepositoryDep, settings: SettingsDep, opts: dict[str, str], publisher: object | None = None
) -> DrainOutcome:
    """Re-ingest + delete every staged lineage event (#4) — the full-event recovery half of the outbox.

    An unparseable (poison) object is dropped so it can't wedge the drain. A well-formed event is ingested
    idempotently (``ingest_event`` MERGEs on ``run_id``) and then deleted; a delete that fails just leaves
    the object for the next tick to re-ingest (a no-op) and retry the delete. Returns the count ingested.

    RUNS BEFORE THE SWEEP, and the order is load-bearing on two counts (§ Q8-16).

    COST: this step is BOUNDED — ``outbox_drain_limit`` events, a handful of object reads — while the
    sweep is O(datasets): measured 2026-09-07 on the live estate, ``checked: 400`` per tick with
    completed sweeps minutes apart against a 30 s cron. Behind the sweep, a committed write's lineage
    stays at risk for as long as the estate is large, which is the wrong variable to depend on.

    FIDELITY: a dataset whose event is still staged looks like drift to the version check, so a sweep
    running first back-fills from STORAGE what the outbox was about to supply from the EVENT —
    recovering a bare version stamp where the staged copy carries inputs, author and columnLineage,
    and reporting a ``backfilled`` finding that would not otherwise have existed.

    PER-EVENT ISOLATION, and it is what makes the sentence above true. One event the graph refuses must
    not decide anything about the others: the failure is caught around a SINGLE event's work, counted as
    STRANDED and named, and the object is deliberately left staged for the next tick. Without it the only
    guard was around the whole drain, so one un-ingestable event stranded every other staged event
    permanently — measured live 2026-09-07 as `Entity failed to be updated: 3` on every sweep, with the
    tick still answering 200 and depth climbing. That inverts the outbox's purpose: the thing built so a
    committed write's lineage survives a crash instead loses every OTHER committed write's lineage.

    STRANDED IS NOT POISON. Poison is unparseable and is dropped; a stranded event is intact and is
    RETRIED, because dropping on a non-validation error destroys the event's only durable copy — the
    2026-07-14 audit finding this module already carries.

    BOUNDED + OBSERVED (docs/DECISIONS.md P1.1/P1.2 — outbox observability + bounded drain). The drain reads
    at most ``outbox_drain_limit`` events per tick, OLDEST FIRST — it previously materialised the whole prefix
    inside the single-flight lock, so a backlog (precisely the situation the outbox exists for) could OOM or
    stall the tick: the relay would fail hardest exactly when it mattered most. The remainder drains next
    tick, so nothing starves. The saturation snapshot is published on EVERY tick — including an empty one, so
    ``outbox.depth`` falls back to 0 instead of going stale at its last non-zero reading and alerting forever.
    """
    depth, oldest_age = await run_in_threadpool(outbox.backlog, settings.outbox_uri, opts)
    outbox_metrics.observe_backlog(depth, oldest_age)
    if depth:
        log.info("lineage_outbox_backlog", extra={"depth": depth, "oldest_age_seconds": round(oldest_age, 1)})

    cap = settings.outbox_drain_limit or None  # 0 => unbounded (the pre-P1.2 behavior)
    staged = await run_in_threadpool(lambda: list(outbox.list_events(settings.outbox_uri, opts, limit=cap)))
    drained = stranded = refused = recorded = 0
    for key, event_json in staged:
        try:
            event = RunEvent.model_validate_json(event_json)
        except ValidationError as exc:
            # ONLY a genuinely-unparseable event is poison. This must stay NARROW (audit 2026-07-14): the
            # broad `except Exception` it replaces deleted the staged object on ANY failure — a transient
            # error would destroy the event's ONLY durable copy, the exact loss #4 exists to prevent.
            # The author rides the RAW json: this branch fires because the event would not validate,
            # so the strict model is unavailable exactly where the answer is needed. Destroying a
            # committed write's only durable copy without recording whose it was is the loss twice over.
            poison_author: str | None = None
            poison_run: str | None = None
            with suppress(Exception):
                payload = json.loads(event_json)
                poison_author = author_sub_from_payload(payload)
                poison_run = str((payload.get("run") or {}).get("runId") or "") or None
            # BOTH, and under names that mean what they say. The staged object's key is
            # `<run_id>@<eventType>`, so stamping it as `run_id` makes this record — the ONE record of a
            # committed write whose only durable copy is being destroyed — uncorrelatable with the run
            # in the graph. The key is what was dropped; the run id is what it was about.
            log.warning(
                "lineage_outbox_poison_dropped",
                extra={"outbox_key": key, "run_id": poison_run, "error": str(exc), "author": poison_author},
            )
            outbox_metrics.record_poison_dropped()
            await run_in_threadpool(outbox.drop_event, settings.outbox_uri, opts, key)
            continue
        try:
            # THE FOURTH INGEST PATH, and it was the only one that authorized nothing. The HTTP door runs
            # `enforce_author` + `enforce_output_authz`, the bus runs `authorize`, the DLQ replay door runs
            # `enforce_output_authz` ("a caller may only replay a run they were authorized to write in the
            # first place") — and this one ingested whatever was staged.
            #
            # THE BYPASS IS BETWEEN SERVICES, not from outside. `enforce_bus_authz` names the threat it
            # closes: the bus is authenticated by the sidecar's shared credential and reads the author off
            # the payload, so a producer holding that token could record any provenance about any dataset.
            # The outbox is writable by exactly those producers (the vended outbox credential, stagers
            # only) while lineage holds `s3:DeleteObject` on the prefix and nothing more — so a stager
            # refused at the bus could stage instead and the relay would ingest it. Not a weaker gate: no
            # gate.
            #
            # THE SAME FUNCTION, never a second copy: it authorizes AS the subject the producer stamped,
            # which is the only principal a cron tick has. Two implementations of "may you record this" is
            # how the doors drifted apart in the first place.
            await enforce_bus_authz(event, request, settings)
            # Graph AND durable feed, in one transaction — see `ingest_event`. The drained run reaching
            # /runs + /producers while SILENTLY absent from /events was the shape this relay exists to
            # prevent, and it is no longer expressible: there is one write.
            await repository.ingest_event(event)  # idempotent — MERGE on run_id, feed ON CONFLICT DO NOTHING
            # RE-PUBLISH, then drop. Ingesting alone repairs the GRAPH and leaves every SUBSCRIBER unaware:
            # medallion's `/bronze-arrival` reacts to this announcement, so a head event recovered but never
            # re-published means provenance is restored while the bronze->silver->gold run it should have
            # started stays halted forever. The relay is the only thing that can restart it.
            #
            # BEFORE the drop, never after: a publish that fails must leave the staged object for the next
            # tick, which is the whole point of staging. The re-ingest on that tick is a no-op (MERGE on
            # run_id), so retrying costs nothing.
            #
            # A duplicate is expected and safe. A staged object can mean "published, then the delete failed",
            # so this may re-deliver something subscribers already saw — which is exactly the at-least-once
            # contract they are built for: the graph MERGEs, the feed is ON CONFLICT DO NOTHING, the inbox
            # keys on `runId@STATE`, and the cascade carries an idempotency token.
            if publisher is not None:
                await dapr_publish.publish_event(
                    publisher,
                    timeout_seconds=settings.dapr_publish_timeout_seconds,
                    pubsub_name=settings.dapr_pubsub,
                    topic_name=settings.dapr_topic,
                    # The STAGED BYTES, never `event.model_dump_json()`. The model is the parsed Python
                    # shape (`run_id`, `event_type`); the wire is OpenLineage (`runId`, `eventType`). Round-
                    # tripping through the model re-publishes a document no subscriber can parse — a silent
                    # corruption of the very event this path exists to save. Byte-identical redelivery is
                    # also the honest thing: subscribers see exactly what they would have seen first time.
                    data=event_json,
                    data_content_type="application/json",
                )
            await run_in_threadpool(outbox.drop_event, settings.outbox_uri, opts, key)
        except PermissionDeniedError as exc:
            # STRANDED, NEVER DROPPED, and under its own name. A refusal is not poison (malformed, wedges
            # the drain forever, so dropped) and not a transient failure (retried next tick): it is a
            # governance answer about a well-formed event, and destroying the only durable copy of a
            # committed write's provenance is the wrong response to "you may not record this".
            #
            # Its own log line because the fact is worth reading: a staged event the graph refuses means a
            # producer is staging provenance it is not authorized to record, which the generic stranded
            # line — shared with credential expiry and store outages — would bury.
            #
            # AND ITS OWN NUMBER, for the same reason one step up. `stranded` is documented as "a tick
            # FAILED while it recovered everything else", and a refusal is the opposite of that: the
            # event is well-formed, the answer is deterministic, and the relay is working exactly as
            # designed. Folding the two together made `drained=0 stranded=6` — measured unchanged for
            # 2.1 days on this estate — read as a wedged relay, so the pair could no longer tell a
            # settled governance answer from the outage it exists to surface.
            refused += 1
            # THE VERIFIED `sub`, parsed HERE from the raw json rather than reused from the poison branch
            # above — `payload` is bound only inside that branch, so reading it here raised
            # `UnboundLocalError`, and because this handler sits inside the per-event `try` it escaped to
            # the tick's error boundary and aborted the WHOLE drain. Not `event.author`: that property
            # prefers the producer-supplied `name` and ownership facet, which is right for attribution on
            # a board and wrong for a loss record, where naming the wrong person is worse than naming
            # nobody (`author_sub_from_payload`). The json already validated into a `RunEvent` on this
            # path, so it parses.
            refused_author = author_sub_from_payload(json.loads(event_json))
            log.warning(
                "lineage_outbox_event_unauthorized",
                extra={"outbox_key": key, "run_id": event.run.run_id, "author": refused_author, "reason": str(exc)},
            )
            # RECORD, THEN RETIRE — the terminal state a settled refusal needs ([[LH-182]]). Counting and
            # logging alone leaves the object, so the same event is re-read, re-parsed, re-refused and
            # re-logged on every tick forever: measured on this estate, `refused=7` unchanged for days.
            #
            # THE ORDER IS THE SAFETY PROPERTY, and only this order is safe. A crash after the insert
            # leaves the object, the next tick re-refuses it, and the upsert on `outbox_key` makes that a
            # no-op. Delete-first would lose the only durable copy of a committed write's provenance if
            # the process died in between — turning a governance answer into silent data loss, which is
            # worse than the loop it replaces.
            #
            # MOVING THE OBJECT ASIDE IS NOT AVAILABLE and that is why this shape exists: a
            # `<outbox>/_refused/` prefix is a PutObject, and the chart grants this service exactly
            # `s3:DeleteObject` on its own outbox (`DrainItsOwnOutboxAndNothingElse`). Building it anyway
            # broke the drain here — the AccessDenied is the tick's error boundary, so it aborted the
            # whole pass and the sweep then reported `refused=0`, a zero meaning "did not look".
            recorded += 1
            await repository.record_refusal(
                outbox_key=key,
                run_id=event.run.run_id,
                author=refused_author,
                reason=str(exc),
                event_json=event_json,
            )
            await run_in_threadpool(outbox.drop_event, settings.outbox_uri, opts, key)
            continue
        except Exception as exc:
            # LEFT STAGED on purpose — see "STRANDED IS NOT POISON" above. Named with both the object key
            # and the run it is about, because the two differ and only one of them finds the run in the graph.
            stranded += 1
            log.warning("lineage_outbox_event_stranded", extra={"outbox_key": key, "run_id": event.run.run_id, "error": str(exc)})
            continue
        drained += 1
    # Always emit — adding 0 CREATES the series, so a dashboard/alert has data from the first tick instead
    # of reading "no data" until the first non-zero drain (the lesson the compaction metrics learned).
    outbox_metrics.record_drained(drained)
    outbox_metrics.record_stranded(stranded)
    outbox_metrics.record_refused(refused)
    # ALL THREE IN THE CONDITION, not just the two that used to exist. Gated on `drained or stranded`,
    # a tick whose only outcome was a REFUSAL logged nothing at all — so splitting the counter would have
    # made the six live refusals invisible and read as the problem disappearing. The line is the thing an
    # operator greps before any dashboard exists, so it carries what the outcome carries.
    if drained or stranded or refused:
        log.info("lineage_outbox_drained", extra={"drained": drained, "stranded": stranded, "refused": refused})
    return DrainOutcome(drained=drained, stranded=stranded, refused=refused, recorded=recorded)


async def _ack_binding() -> dict[str, str]:
    """Dapr's startup pre-flight (OPTIONS /<binding-name>) — a 2xx confirms this app consumes the binding."""
    return {"status": "ok"}


def build_reconcile_cron_router(binding_name: str) -> APIRouter:
    """Register the cron route at the exact binding name the sidecar delivers to (POST sweep, OPTIONS ack)."""
    router = APIRouter()
    router.add_api_route(f"/{binding_name}", _on_cron, methods=["POST"], tags=["reconcile"])
    router.add_api_route(f"/{binding_name}", _ack_binding, methods=["OPTIONS"], include_in_schema=False)
    return router
