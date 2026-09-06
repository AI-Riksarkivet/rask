"""The cascade-lag cron door — one tick per firing, read-only.

docs/DECISIONS.md "Cascade repair" (C3). The tick itself (`services/cascade_lag.py`) is pure over two readers; this
is the door the Dapr cron binding drives and the place the real catalog and lineage reads are wired.

ONE STRING, THREE TIMES. Dapr delivers an input binding to ``POST /<component-name>`` at the pod ROOT,
never under ``MEDALLION_API_PREFIX``. So the Component's ``metadata.name``, this service's setting and
the path served here are the same string, and any two agreeing while the third does not is a cron that
fires into a 404 forever with every pod green.

OPT-IN on a configured name, like the control relay and lineage's reconcile route: an unnamed binding
means a deployment with no component or no sidecar, and an always-live door for it is a surface with
nothing behind it.

THE TICK IS BLOCKING WORK AND RUNS IN A THREADPOOL. Its readers are synchronous per-edge HTTP,
so an `async` door that awaited nothing held the event loop for the whole scan and the pod's own
probes timed out under it — see `_on_cron`.

READ-ONLY, WHICH IS THE WHOLE REPLICA ANSWER. ``bindings.cron`` fires on every replica with no lease.
This tick computes a level and sets a gauge; two replicas compute the same level and set it twice,
which is what a gauge tolerates. No lock, no ``replicas: 1`` pin, no dedupe key — see the reasoning on
``run_lag_tick``.
"""

from __future__ import annotations

from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI
from starlette.concurrency import run_in_threadpool

from medallion.api.dependencies import SettingsDep
from medallion.core.metrics import cascade_lag_gauge
from medallion.services.cascade_lag import LagTickReport, run_lag_tick
from service_kit.governed.dapr_auth import require_dapr_token


async def _on_cron(settings: SettingsDep, _: Annotated[None, Depends(require_dapr_token)]) -> LagTickReport:
    """One lag tick.

    Guarded by the Dapr app token so only the sidecar's cron may drive it: unauthenticated, anything
    that can reach the port could ask for a full catalog-and-lineage scan on demand.
    """
    from medallion.services.cascade_lag_readers import consumed_reader, declared_edges, published_reader

    # OFF THE EVENT LOOP. Both readers are synchronous `httpx.get` calls issued once per declared
    # edge (`cascade_lag_readers.py`), so a tick is one blocking round-trip per governed tenant.
    # Measured on `rask-medallion-producer` 2026-09-06: one tick spanned fifteen seconds, and run
    # inline it held the loop for all of it — 21 readiness and 7 liveness probe timeouts in 22
    # minutes against `timeoutSeconds: 1`. NotReady drops the pod from its Service endpoints (a
    # healthy producer reported unreachable, which made the governed-union e2e skip on one run and
    # execute on the next), and three consecutive liveness failures would restart the container
    # mid-cascade. The tick is not too slow; it was in the wrong place.
    #
    # `declared_edges` and the two reader FACTORIES are called out here rather than inside the
    # thread on purpose: they only assemble closures over settings, and keeping them at this level
    # leaves the threadpool holding exactly the blocking work.
    return await run_in_threadpool(
        partial(
            run_lag_tick,
            edges=declared_edges(settings),
            published=published_reader(settings),
            consumed=consumed_reader(settings),
            gauge=cascade_lag_gauge(),
        )
    )


async def _ack_binding() -> dict[str, str]:
    """The sidecar probes with OPTIONS before delivering; a POST-only door is reported unroutable."""
    return {"status": "ok"}


def build_lag_cron_router(binding_name: str) -> APIRouter:
    """A FACTORY, not a module-level router: the path is the binding NAME, known only at wiring time."""
    router = APIRouter()
    router.add_api_route(f"/{binding_name}", _on_cron, methods=["POST"], tags=["cascade-lag"])
    router.add_api_route(f"/{binding_name}", _ack_binding, methods=["OPTIONS"], include_in_schema=False)
    return router


def mount_lag_cron(app: FastAPI, binding_name: str) -> bool:
    """Mount the lag cron when a binding name is configured; report whether it was."""
    if not binding_name:
        return False
    app.include_router(build_lag_cron_router(binding_name))
    return True
