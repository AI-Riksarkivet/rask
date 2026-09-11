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

    A model rather than a hand-built ``dict[str, Any]``: the tick reports EIGHT independent finding classes
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
    outbox_drained: int = 0
    outbox_stranded: int = 0
    pruned_runs: int = 0
    pruned_events: int = 0


class DrainOutcome(BaseModel):
    """What one outbox drain did — BOTH numbers, because either alone reads as the opposite of the truth.

    ``drained`` alone says a tick succeeded while a specific event has been refused on every tick since
    the estate came up; ``stranded`` alone says a tick failed while it recovered everything else. The
    pair is what distinguishes "the relay is working and one event needs a human" from "the relay is
    wedged", and those need different responses.
    """

    drained: int = 0
    stranded: int = 0


def summarize_sweep(statuses: list[ReconcileStatus]) -> SweepReport:
    """Partition one sweep's statuses into the tick's finding classes.

    Pure and public so the unit tier can drive a partition from a handful of statuses instead of standing
    up a whole sweep. ``outbox_drained`` / ``pruned_runs`` / ``pruned_events`` are not derived from
    statuses — the caller stamps them on.
    """
    return SweepReport(
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
            "outbox_drained": report.outbox_drained,
            "outbox_stranded": report.outbox_stranded,
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


async def _sweep(repository: RepositoryDep, settings: SettingsDep, opts: dict[str, str], governed: set[str] | None = None) -> list[ReconcileStatus]:
    """Reconcile every dataset against storage, back-filling any write whose lineage event was lost.

    The Lance reads all run in the threadpool so the object-store I/O never stalls the event loop.
    """
    return await reconcile_all(
        repository,
        lambda uri: run_in_threadpool(read_storage_version, uri, opts),
        backfill=True,
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
    # probing them. Measured 2026-09-08 — 1,271 Dataset nodes, every one still edge-reachable — which is
    # why this cannot be a standalone sweep: a dataset becomes prunable exactly when its last run goes.
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
        report = summarize_sweep(await _sweep(repository, settings, opts, await governed_tables(request, settings)))
        report.outbox_drained, report.outbox_stranded = outcome.drained, outcome.stranded
        report.pruned_runs = await _prune_old_runs(repository, settings)
        report.pruned_events = await _prune_old_events(repository, settings)
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
    drained = stranded = 0
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
            stranded += 1
            log.warning(
                "lineage_outbox_event_unauthorized",
                extra={"outbox_key": key, "run_id": event.run.run_id, "author": author_sub_from_payload(payload), "reason": str(exc)},
            )
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
    if drained or stranded:
        log.info("lineage_outbox_drained", extra={"drained": drained, "stranded": stranded})
    return DrainOutcome(drained=drained, stranded=stranded)


async def _ack_binding() -> dict[str, str]:
    """Dapr's startup pre-flight (OPTIONS /<binding-name>) — a 2xx confirms this app consumes the binding."""
    return {"status": "ok"}


def build_reconcile_cron_router(binding_name: str) -> APIRouter:
    """Register the cron route at the exact binding name the sidecar delivers to (POST sweep, OPTIONS ack)."""
    router = APIRouter()
    router.add_api_route(f"/{binding_name}", _on_cron, methods=["POST"], tags=["reconcile"])
    router.add_api_route(f"/{binding_name}", _ack_binding, methods=["OPTIONS"], include_in_schema=False)
    return router
