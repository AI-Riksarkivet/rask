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

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError

from lineage.api.dependencies import PublisherDep, RepositoryDep, SettingsDep
from lineage.core.config import declared_columns_map, storage_options
from lineage.core.reconcile import (
    BACKFILLABLE_STATES,
    STORAGE_LOSS_STATES,
    read_dangling_blob_columns,
    read_latest_write_age_hours,
    read_storage_schema,
    read_storage_version,
    reconcile_all,
)
from lineage.models import RunEvent, author_sub_from_payload
from lineage.schemas import ReconcileState, ReconcileStatus
from lineage.services.consumer import record_event_best_effort
from service_kit import dapr_publish
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.lakehouse import outbox, outbox_metrics


log = logging.getLogger(__name__)


class SweepReport(BaseModel):
    """One cron tick's findings — the tick's response body and the shape its log line counts.

    A model rather than a hand-built ``dict[str, Any]``: the tick reports SIX independent finding classes
    plus two counters, and the response was assembled twice in one function body (once as a log ``extra``,
    once as the return) from literal keys that could drift apart silently.
    """

    checked: int = 0
    backfilled: list[str] = Field(default_factory=list)
    storage_loss: list[str] = Field(default_factory=list)
    unreadable: dict[str, str | None] = Field(default_factory=dict)
    dangling_blobs: dict[str, list[str]] = Field(default_factory=dict)
    stale: list[str] = Field(default_factory=list)
    contract_violations: dict[str, list[str]] = Field(default_factory=dict)
    outbox_drained: int = 0
    pruned_runs: int = 0


def summarize_sweep(statuses: list[ReconcileStatus]) -> SweepReport:
    """Partition one sweep's statuses into the tick's finding classes.

    Pure and public so the unit tier can drive a partition from a handful of statuses instead of standing
    up a whole sweep. ``outbox_drained`` / ``pruned_runs`` are not derived from statuses — the caller
    stamps them on.
    """
    return SweepReport(
        checked=len(statuses),
        backfilled=[s.dataset for s in statuses if s.status in BACKFILLABLE_STATES],
        storage_loss=[s.dataset for s in statuses if s.status in STORAGE_LOSS_STATES],
        unreadable={s.dataset: s.unreadable_reason for s in statuses if s.status is ReconcileState.UNREADABLE},
        dangling_blobs={s.dataset: s.dangling_blob_columns for s in statuses if s.dangling_blob_columns},
        stale=[s.dataset for s in statuses if s.stale],
        contract_violations={s.dataset: s.missing_declared_columns for s in statuses if s.missing_declared_columns},
    )


def log_sweep(report: SweepReport) -> None:
    """Emit one WARN per non-empty finding class, then the tick's INFO summary.

    Every class here is a finding the sweep CANNOT auto-fix, which is why each gets its own line rather
    than a count buried in the summary:

    * ``storage_loss`` — the graph claims data on-disk Lance no longer has (a bad restore, a wipe). The
      data is gone; only a human can answer for it.
    * ``unreadable`` — datasets this reader could not OPEN, reported on their own line and deliberately
      NOT counted as loss: "we could not read it" and "it is gone" demand opposite responses. Before
      they were separated, six live datasets carrying an unsupported manifest feature flag were reported
      as destroyed, in the same hour ``services/maintenance`` was reading their manifests. WARN rather
      than ERROR because the data is very likely fine; what is broken is our ability to see it, and the
      reason says which.
    * ``dangling_blobs`` — payloads gone from under a version-wise-healthy table; the bytes are lost.
    * ``stale`` — data stopped arriving inside the freshness budget; the fix is upstream.
    * ``contract_violations`` — a dataset's CURRENT schema lost a column a consumer declared, i.e. a
      write that bypassed the mover skipped the gate.
    """
    if report.storage_loss:
        log.warning("lineage_reconcile_storage_loss", extra={"datasets": report.storage_loss, "count": len(report.storage_loss)})
    if report.unreadable:
        log.warning("lineage_reconcile_unreadable", extra={"datasets": report.unreadable, "count": len(report.unreadable)})
    if report.dangling_blobs:
        log.warning("lineage_reconcile_dangling_blobs", extra={"datasets": report.dangling_blobs, "count": len(report.dangling_blobs)})
    if report.stale:
        log.warning("lineage_reconcile_stale", extra={"datasets": report.stale, "count": len(report.stale)})
    if report.contract_violations:
        log.warning("lineage_reconcile_contract_violation", extra={"datasets": report.contract_violations, "count": len(report.contract_violations)})
    log.info(
        "lineage_reconcile_sweep",
        extra={
            "checked": report.checked,
            "backfilled": len(report.backfilled),
            "storage_loss": len(report.storage_loss),
            "unreadable": len(report.unreadable),
            "dangling_blobs": len(report.dangling_blobs),
            "stale": len(report.stale),
            "contract_violations": len(report.contract_violations),
            "outbox_drained": report.outbox_drained,
            "pruned_runs": report.pruned_runs,
        },
    )


async def _sweep(repository: RepositoryDep, settings: SettingsDep, opts: dict[str, str]) -> list[ReconcileStatus]:
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
        freshness_budget_hours=settings.freshness_budget_hours,
        # Declared-columns patrol (Batch 23): re-check the gate's column_declared assertion
        # estate-wide — only declared datasets pay the schema read.
        declared=declared_columns_map(settings),
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
    return pruned


async def _on_cron(
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
        report = summarize_sweep(await _sweep(repository, settings, opts))
        # Drain the lineage OUTBOX (#4): re-ingest any event a producer STAGED but whose publish never got
        # acked — a crash between the Lance commit and the fire-and-forget publish — then delete it. Unlike
        # the version+schema back-fill above, this recovers the FULL event (inputs, author, columnLineage).
        # Idempotent: ingest_event MERGEs on run_id, so a redundant republish (publish DID land, producer
        # crashed before deleting) is a no-op. Runs inside the same single-flight lock — no double-drain.
        if settings.outbox_uri:
            try:
                report.outbox_drained = await _drain_outbox(repository, settings, opts, publisher)
            except Exception as exc:
                log.warning("lineage_outbox_drain_failed", extra={"error": str(exc)})
        report.pruned_runs = await _prune_old_runs(repository, settings)
    log_sweep(report)
    return report.model_dump()


async def _drain_outbox(repository: RepositoryDep, settings: SettingsDep, opts: dict[str, str], publisher: object | None = None) -> int:
    """Re-ingest + delete every staged lineage event (#4) — the full-event recovery half of the outbox.

    An unparseable (poison) object is dropped so it can't wedge the drain. A well-formed event is ingested
    idempotently (``ingest_event`` MERGEs on ``run_id``) and then deleted; a delete that fails just leaves
    the object for the next tick to re-ingest (a no-op) and retry the delete. Returns the count ingested.

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
    drained = 0
    for run_id, event_json in staged:
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
            with suppress(Exception):
                poison_author = author_sub_from_payload(json.loads(event_json))
            log.warning(
                "lineage_outbox_poison_dropped",
                extra={"run_id": run_id, "error": str(exc), "author": poison_author},
            )
            outbox_metrics.record_poison_dropped()
            await run_in_threadpool(outbox.drop_event, settings.outbox_uri, opts, run_id)
            continue
        await repository.ingest_event(event)  # idempotent — MERGE on run_id (authoritative AGE graph)
        # Mirror BOTH live ingest paths (JetStream consumer + HTTP ingest): also project onto the durable
        # `lineage_events` feed. Without this the drained run reaches /runs + /producers but is SILENTLY
        # absent from the /events audit surface — exactly the run the outbox exists to save. The feed INSERT
        # is ON CONFLICT DO NOTHING on the natural key, so a later genuine redelivery won't duplicate.
        await record_event_best_effort(repository, event)
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
        await run_in_threadpool(outbox.drop_event, settings.outbox_uri, opts, run_id)
        drained += 1
    # Always emit — adding 0 CREATES the series, so a dashboard/alert has data from the first tick instead
    # of reading "no data" until the first non-zero drain (the lesson the compaction metrics learned).
    outbox_metrics.record_drained(drained)
    if drained:
        log.info("lineage_outbox_drained", extra={"drained": drained})
    return drained


async def _ack_binding() -> dict[str, str]:
    """Dapr's startup pre-flight (OPTIONS /<binding-name>) — a 2xx confirms this app consumes the binding."""
    return {"status": "ok"}


def build_reconcile_cron_router(binding_name: str) -> APIRouter:
    """Register the cron route at the exact binding name the sidecar delivers to (POST sweep, OPTIONS ack)."""
    router = APIRouter()
    router.add_api_route(f"/{binding_name}", _on_cron, methods=["POST"], tags=["reconcile"])
    router.add_api_route(f"/{binding_name}", _ack_binding, methods=["OPTIONS"], include_in_schema=False)
    return router
