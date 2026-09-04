"""The cascade-lag cron door — one tick per firing, read-only.

`open_cascade_repair.md` C3. The tick itself (`services/cascade_lag.py`) is pure over two readers; this
is the door the Dapr cron binding drives and the place the real catalog and lineage reads are wired.

ONE STRING, THREE TIMES. Dapr delivers an input binding to ``POST /<component-name>`` at the pod ROOT,
never under ``MEDALLION_API_PREFIX``. So the Component's ``metadata.name``, this service's setting and
the path served here are the same string, and any two agreeing while the third does not is a cron that
fires into a 404 forever with every pod green.

OPT-IN on a configured name, like the control relay and lineage's reconcile route: an unnamed binding
means a deployment with no component or no sidecar, and an always-live door for it is a surface with
nothing behind it.

READ-ONLY, WHICH IS THE WHOLE REPLICA ANSWER. ``bindings.cron`` fires on every replica with no lease.
This tick computes a level and sets a gauge; two replicas compute the same level and set it twice,
which is what a gauge tolerates. No lock, no ``replicas: 1`` pin, no dedupe key — see the reasoning on
``run_lag_tick``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI

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

    return run_lag_tick(
        edges=declared_edges(settings),
        published=published_reader(settings),
        consumed=consumed_reader(settings),
        gauge=cascade_lag_gauge(),
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
