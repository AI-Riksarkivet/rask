"""The Dapr cron HTTP surface: the binding-name POST route (runs one sweep) + its OPTIONS ack.

A ``bindings.cron`` component POSTs to ``/<binding-name>`` every interval; OPTIONS is Dapr's
binding-discovery pre-flight. The POST is guarded by ``require_dapr_token`` so only the sidecar's cron
tick may trigger a sweep. Blocking Lance/S3 IO runs in the threadpool so the event loop stays free.
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from compaction.api.dependencies import LineageEmitterDep, SettingsDep
from compaction.core.config import get_settings
from compaction.services.sweep import emit_sweep_lineage, run_sweep, summarize
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)

router = APIRouter()

# Single-flight guard: the sweep is unbounded (it discovers + compacts EVERY dataset), so a slow sweep can
# outlast the cron interval. Without this, the next tick starts a SECOND concurrent sweep and the two race
# compact_files()/cleanup_old_versions() on the same datasets (concurrent commits + a GC deleting versions
# the other is reading). Module-level asyncio.Lock created without binding a loop (py3.10+); with
# compactionReplicas=1 (values.yaml) this is cluster-wide single-flight. The reconcile sweep does the same
# with a pg advisory lock — compaction is stateless (no DB), so an in-process lock is the analog. A tick
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
        log.warning("compaction_sweep_skipped", extra={"reason": "previous sweep still running"})
        return {"status": "skipped", "reason": "overlapping sweep still running"}
    async with _sweep_lock:
        results = await run_in_threadpool(run_sweep, settings)
        await emit_sweep_lineage(emitter, results, delimiter=settings.delimiter)
        summary = summarize(results)
        log.info("compaction_sweep", extra=summary)
        return summary


async def ack_binding() -> dict[str, str]:
    """Dapr's startup pre-flight (OPTIONS /<binding-name>) — a 2xx confirms this app consumes the binding."""
    return {"status": "ok"}


# Register the cron route at the exact binding name the sidecar delivers to: POST runs the sweep, OPTIONS
# acks Dapr's binding-discovery pre-flight (else it 405s and Dapr logs the app as not consuming it).
_binding_name = get_settings().binding_name
router.add_api_route(
    f"/{_binding_name}",
    on_cron,
    methods=["POST"],
    tags=["compaction"],
    dependencies=[Depends(require_dapr_token)],  # only the sidecar's cron tick may trigger a sweep
)
router.add_api_route(f"/{_binding_name}", ack_binding, methods=["OPTIONS"], include_in_schema=False)
