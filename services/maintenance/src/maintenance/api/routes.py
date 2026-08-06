"""The Dapr cron HTTP surface: TWO binding-name POST routes + their OPTIONS acks.

A ``bindings.cron`` component POSTs to ``/<binding-name>`` every interval; OPTIONS is Dapr's
binding-discovery pre-flight. Both POSTs are guarded by ``require_dapr_token`` so only the sidecar's
cron tick may trigger them. Blocking Lance/S3 IO runs in the threadpool so the event loop stays free.

Two bindings, not one route doing both:

* the **sweep** (`binding_name`) compacts, cleans up old versions and optimizes indices — it REWRITES
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

from maintenance.api.dependencies import ControlEmitterDep, FgaClientDep, LineageEmitterDep, S3ClientDep, SettingsDep
from maintenance.core.config import get_settings
from maintenance.services.purge import purge_expired_trash
from maintenance.services.reconcile import reconcile
from maintenance.services.sweep import emit_sweep_lineage, run_sweep, summarize
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)

router = APIRouter()

# Single-flight guard: the sweep is unbounded (it discovers + compacts EVERY dataset), so a slow sweep can
# outlast the cron interval. Without this, the next tick starts a SECOND concurrent sweep and the two race
# compact_files()/cleanup_old_versions() on the same datasets (concurrent commits + a GC deleting versions
# the other is reading). Module-level asyncio.Lock created without binding a loop (py3.10+); with
# compactionReplicas=1 (values.yaml) this is cluster-wide single-flight. The reconcile sweep does the same
# with a pg advisory lock — maintenance is stateless (no DB), so an in-process lock is the analog. A tick
# that finds a sweep already running SKIPS (does not queue): the running sweep already covers every dataset,
# so re-running is redundant, and queuing would pile ticks up behind a long sweep.
_sweep_lock = asyncio.Lock()


async def on_cron(settings: SettingsDep, emitter: LineageEmitterDep) -> dict[str, Any]:
    """One maintenance sweep, triggered by a Dapr cron tick (POST /<binding-name>).

    Single-flight: if a prior tick's sweep is still running, SKIP this one (it would only re-cover the same
    datasets and race the running sweep). The blocking discover + compact/GC runs in the threadpool; then
    each materially-compacted dataset records a maintenance run on the lineage graph (#7b) — awaited inline
    so the publish reaches the durable Dapr/JetStream transport before we return, and best-effort so it never
    fails the sweep.
    """
    if _sweep_lock.locked():
        log.warning("maintenance_sweep_skipped", extra={"reason": "previous sweep still running"})
        return {"status": "skipped", "reason": "overlapping sweep still running"}
    async with _sweep_lock:
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


# Register the cron route at the exact binding name the sidecar delivers to: POST runs the sweep, OPTIONS
# acks Dapr's binding-discovery pre-flight (else it 405s and Dapr logs the app as not consuming it).
_settings = get_settings()
_binding_name = _settings.binding_name
router.add_api_route(
    f"/{_binding_name}",
    on_cron,
    methods=["POST"],
    tags=["maintenance"],
    dependencies=[Depends(require_dapr_token)],  # only the sidecar's cron tick may trigger a sweep
)
router.add_api_route(f"/{_binding_name}", ack_binding, methods=["OPTIONS"], include_in_schema=False)

# The reconcile binding, same shape and the same token gate. It reads every tenant's registry records
# and tuple counts, so an unauthenticated trigger would be an estate-wide disclosure, not just a wasted
# scan — the gate is load-bearing here even though the pass mutates nothing.
_reconcile_binding_name = _settings.reconcile_binding_name
router.add_api_route(
    f"/{_reconcile_binding_name}",
    on_reconcile_cron,
    methods=["POST"],
    tags=["maintenance"],
    dependencies=[Depends(require_dapr_token)],
)
router.add_api_route(f"/{_reconcile_binding_name}", ack_reconcile_binding, methods=["OPTIONS"], include_in_schema=False)
