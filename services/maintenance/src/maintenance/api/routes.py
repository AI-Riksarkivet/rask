"""The Dapr cron HTTP surface: TWO binding-name POST routes + their OPTIONS acks.

A ``bindings.cron`` component POSTs to ``/<binding-name>`` every interval; OPTIONS is Dapr's
binding-discovery pre-flight. Both POSTs are guarded by ``require_dapr_token`` so only the sidecar's
cron tick may trigger them. Blocking Lance/S3 IO runs in the threadpool so the event loop stays free.

Two bindings, not one route doing both:

* the **sweep** (`binding_name`) compacts, optimizes indices and cleans up old versions — it REWRITES
  data files and is expensive;
* the **reconcile** pass (`reconcile_binding_name`) reads three stores and reports their
  disagreements — cheap — and then, gated on THAT report running clean, reclaims expired trash (#79).

One binding would force the cheap read onto the expensive write's schedule, which is exactly the
constraint that keeps drift reports rare enough to be useless.

The purge rides the reconcile binding rather than the sweep's precisely because its permission comes
from the report: it consumes the report object the same tick produced, never a stored one. It is off by
default (`MAINTENANCE_TRASH_PURGE_ENABLED`), so the shipped configuration is still report-only.
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from maintenance.api.dependencies import ControlEmitterDep, DaprClientDep, FgaClientDep, LineageEmitterDep, S3ClientDep, SettingsDep
from maintenance.core.config import MaintenanceSettings
from maintenance.core.metrics import record_run
from maintenance.services.purge import purge_expired_trash
from maintenance.services.reconcile import reconcile
from maintenance.services.sweep import emit_sweep_lineage, plan_sweep, run_sweep, summarize
from maintenance.services.work_queue import enqueue_units
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)

# Single-flight guard: the sweep is unbounded (it discovers + compacts EVERY dataset), so a slow sweep can
# outlast the cron interval. Without this, the next tick starts a SECOND concurrent sweep and the two race
# compact_files()/cleanup_old_versions() on the same datasets (concurrent commits + a GC deleting versions
# the other is reading). Module-level asyncio.Lock created without binding a loop (py3.10+); with
# ONE replica this is cluster-wide single-flight — which holds today, but not for the reason this comment
# used to give. It cited `compactionReplicas=1 (values.yaml)`; that key EXISTS NOWHERE in the chart
# (diff2 F10 item 7), so the citation was itself drift. What actually pins it is
# `chart/templates/maintenance.yaml:47`, which HARDCODES `replicas: 1` — scaling is not reachable through
# values at all, only by a kubectl scale or a template edit. So the lock is safe by accident of an
# unparameterised template, and anyone who adds that values key without also making this lock
# distributed silently gets two concurrent sweeps racing compact_files()/cleanup_old_versions() on the
# same datasets. That coupling is now MECHANICAL rather than planned:
# tests/unit/test_invariants.py::test_the_sweep_lock_is_only_correct_while_maintenance_CANNOT_scale
# fails the moment `replicas` stops being a literal 1 while this lock is still an `asyncio.Lock`, so
# parameterising the deployment forces the distributed-lock conversation instead of silently starting a
# second concurrent sweep. (The guard keys on the LOCK, so replacing it lifts the restriction.)
# The reconcile sweep does the same
# with a pg advisory lock — maintenance is stateless (no DB), so an in-process lock is the analog. A tick
# that finds a sweep already running SKIPS (does not queue): the running sweep already covers every dataset,
# so re-running is redundant, and queuing would pile ticks up behind a long sweep.
_sweep_lock = asyncio.Lock()


async def on_cron(settings: SettingsDep, emitter: LineageEmitterDep, dapr: DaprClientDep) -> dict[str, Any]:
    """One maintenance tick, triggered by a Dapr cron tick (POST /<binding-name>).

    TWO LANES, and which one runs is decided by whether there IS a queue rather than by a flag — so a
    production deployment has exactly one path instead of two that can drift:

    * **A work topic is configured** → this tick PLANS and enqueues, and maintains nothing itself. It
      returns as soon as the units are published, so the handler's cost is the estate's dataset COUNT
      rather than its size. A unit that fails to publish is reported in the response, not swallowed:
      it is simply not maintained this tick and the next one re-plans it.
    * **No work topic** (local runs, the test suite) → the serial sweep, unchanged.

    Single-flight either way: an overlapping tick SKIPS. It matters less on the queue lane — planning is
    bounded — but two concurrent planners would enqueue every dataset twice, and duplicate units are
    wasted work even though they are safe (compaction and GC are convergent).

    The serial lane's emit phase is awaited as one bounded, concurrent batch — per-dataset serial awaits
    were the MAINT-04 defect — so every publish reaches the durable Dapr/JetStream transport before we
    return, and it is best-effort throughout, so a publish failure never fails the tick. On the queue
    lane each unit emits its own lineage from the subscription that executed it.
    """
    if _sweep_lock.locked():
        log.warning("maintenance_sweep_skipped", extra={"reason": "previous sweep still running"})
        return {"status": "skipped", "reason": "overlapping sweep still running"}
    async with _sweep_lock:
        if settings.work_topic and dapr is not None:
            items, decided = await run_in_threadpool(plan_sweep, settings)
            published, not_queued = await enqueue_units(
                dapr, items, pubsub=settings.work_pubsub, topic=settings.work_topic, timeout_seconds=settings.publish_timeout_seconds
            )
            # The trash exclusions are decided WITHOUT work and are results, not units — they must not be
            # enqueued, and their lineage is emitted here because no subscription will ever see them.
            await emit_sweep_lineage(emitter, decided, delimiter=settings.delimiter)
            summary = {"status": "enqueued", "planned": len(items), "published": published, "not_queued": len(not_queued), "skipped": len(decided)}
            # THE COMPLETION HALF OF THE PAIR `plan_sweep` OPENED. `record_run_started` fires inside
            # `plan_sweep`, which both lanes call; `record_run` used to fire only inside `run_sweep`,
            # which only the serial lane calls — so on the lane every deployment actually runs, the pair
            # opened and never closed and `compaction_runs_total` never existed. Measured on the deployed
            # estate 2026-09-06: started 54, datasets swept 7937, `absent(compaction_runs_total)` = 1, so
            # the critical MaintenanceSweepMetricsMissing was paging while nothing was wrong and
            # MaintenanceSweepNotCompleting could not fire at all.
            #
            # A QUEUE TICK COMPLETES WHEN THE ESTATE IS PLANNED AND THE UNITS ARE DURABLY PUBLISHED, not
            # when they finish: the units are executed later by subscriptions that ack for themselves.
            # Both alerts ask "is the sweep running at all", and on this lane the planner IS the sweep.
            # Counting unit execution would emit hundreds of completions per tick and break the pairing
            # with `started` that the rules' lost-pass arithmetic depends on.
            record_run()
            log.info("maintenance_tick_enqueued", extra=summary)
            return summary
        results = await run_in_threadpool(run_sweep, settings)
        await emit_sweep_lineage(emitter, results, delimiter=settings.delimiter)
        summary = summarize(results)
        log.info("maintenance_sweep", extra=summary)
        return summary


async def ack_binding() -> dict[str, str]:
    """Dapr's startup pre-flight (OPTIONS /<binding-name>) — a 2xx confirms this app consumes the binding."""
    return {"status": "ok"}


# Single-flight for the reconcile pass, for a different reason than the sweep's: the report is READ-ONLY,
# so overlapping runs cannot corrupt anything — they would just spend three stores' read budget twice and
# emit two reports a human has to reconcile. Skip, don't queue: the next tick re-reads current state anyway.
_reconcile_lock = asyncio.Lock()


async def on_reconcile_cron(settings: SettingsDep, client: FgaClientDep, bucket_client: S3ClientDep, control: ControlEmitterDep) -> dict[str, Any]:
    """One cross-store drift REPORT, then — gated on it — the #79 expired-trash purge.

    Separate binding from the sweep because the cadences differ by an order of magnitude: the sweep
    rewrites data files, this only reads three stores.

    The report is returned AND logged as one structured record, because the two have different readers —
    the sidecar's response goes nowhere a human looks, while the log lands in the OTLP stream where drift
    is actually noticed. `total` is logged at WARNING when non-zero so a clean estate stays quiet and a
    drifting one does not.

    **The purge lives HERE, not on the sweep tick, and inside the SAME lock.** Its gate is "the drift
    report ran clean", so it must consume the report THIS tick produced — a purge reading a stored or
    previous report would certify a state that no longer exists. Reclamation is off by default
    (`MAINTENANCE_TRASH_PURGE_ENABLED`), so on the shipped configuration this adds one report key and
    deletes nothing.
    """
    if _reconcile_lock.locked():
        log.warning("reconcile_skipped", extra={"reason": "previous reconcile still running"})
        return {"status": "skipped", "reason": "overlapping reconcile still running"}
    async with _reconcile_lock:
        report = await reconcile(
            settings,
            client,
            warehouses_enabled=settings.warehouses_enabled,
            control_root=settings.resolved_control_root,
            fga_root_object=settings.fga_root_object,
            bucket_client=bucket_client,
        )
        payload = report.model_dump(mode="json")
        summary = {
            "total": report.total,
            "counts": report.counts,
            "unavailable": [u.category for u in report.unavailable],
            "skipped": [s.category for s in report.skipped],
            "incomplete": [i.source for i in report.incomplete],
        }
        # A category that could not be checked is NOT clean, so an unavailable/incomplete run is as
        # loud as a drifting one — otherwise a permanently-broken FGA connection reads as "no drift".
        if report.total or report.unavailable or report.incomplete:
            log.warning("reconcile_drift", extra=summary)
        else:
            log.info("reconcile_clean", extra=summary)
        purged = await purge_expired_trash(
            settings,
            report=report,
            fga_client=client,
            control=control,
            control_root=settings.resolved_control_root,
        )
        payload["trash_purge"] = purged.model_dump(mode="json")
        # Reclamation is irreversible, so a tick that destroyed something — or declined to — says so at
        # WARNING. A quiet INFO would put the one irreversible thing this service does at the same
        # volume as a no-op sweep.
        if purged.purged or purged.refused or purged.capped:
            log.warning(
                "trash_purge_result",
                extra={
                    "purged": [f"{p.kind}:{p.id}" for p in purged.purged],
                    "refused": [{"id": f"{r.kind}:{r.id}", "reason": r.reason} for r in purged.refused],
                    "capped": purged.capped,
                    "bytes_reclaimed": sum(p.bytes_deleted for p in purged.purged),
                },
            )
        return payload


async def ack_reconcile_binding() -> dict[str, str]:
    """Dapr's OPTIONS pre-flight for the reconcile binding."""
    return {"status": "ok"}


def build_router(settings: MaintenanceSettings) -> APIRouter:
    """The two cron routes, at the exact binding names the sidecar delivers to.

    A FACTORY, not a module-level router, and the difference is not cosmetic (MAINT-13). This module
    used to call `get_settings()` and `add_api_route` at import time, so the binding names were frozen
    by whatever environment the first import saw — `MAINTENANCE_BINDING_NAME` was a setting nothing
    could actually drive, and an app assembled from different settings than the module had cached
    would serve paths Dapr never POSTs to, with no error anywhere. Taking the settings as an argument
    makes the route topology a function of its input, which is also the only way a test can assert it.

    Each binding gets a POST (the work) and an OPTIONS (Dapr's binding-discovery pre-flight — without
    the 2xx it 405s and Dapr logs the app as not consuming the binding). Both POSTs carry
    `require_dapr_token`: only the sidecar's cron tick may trigger a sweep, and the reconcile pass
    reads every tenant's registry records and tuple counts, so an unauthenticated trigger there would
    be an estate-wide disclosure rather than just a wasted scan — the gate is load-bearing even though
    that pass mutates nothing.

    The tag is declared ONCE, on the router that owns both routes, rather than repeated per route.
    """
    router = APIRouter(tags=["maintenance"])
    for name, on_post, on_options in (
        (settings.binding_name, on_cron, ack_binding),
        (settings.reconcile_binding_name, on_reconcile_cron, ack_reconcile_binding),
    ):
        # AN UNNAMED BINDING MOUNTS NOTHING. Unconditional, this served `f"/{''}"` — a token-guarded
        # cron door at the pod ROOT — which an EXECUTOR pod (M1: no cron, work queue only) would have
        # published by simply not configuring a binding. Opt-in on a configured name is the same rule
        # the control relay and the cascade-lag cron already follow.
        if not name:
            continue
        router.add_api_route(f"/{name}", on_post, methods=["POST"], dependencies=[Depends(require_dapr_token)])
        router.add_api_route(f"/{name}", on_options, methods=["OPTIONS"], include_in_schema=False)
    return router
