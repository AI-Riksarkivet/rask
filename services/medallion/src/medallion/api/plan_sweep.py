"""The plan sweep's cron door, ``POST /<binding name>`` at the pod root (CP-029 D-6), for whichever lane an app plans.

ONE STRING, THREE TIMES: the Dapr Component's ``metadata.name``, `MEDALLION_PLAN_SWEEP_BINDING_NAME` and the path
served here, all rendered from one chart value, so a cron cannot fire into a 404 with every pod green. One Component
is scoped to every app that plans (each stage runner and the producer), and each sidecar delivers to its own app,
whose sweep reads only the plans that app owns. Guarded by the Dapr app token, so only the sidecar's cron drives an
engine read per open plan. It fires on every replica and needs no lease: every plan write is CAS.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from medallion.api.dependencies import SettingsDep
from medallion.core.config import MedallionSettings
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.lakehouse.run_outcomes import SweepReport


#: One lane's sweep: the app's settings and its Dapr client in, what the tick did out.
type Sweep = Callable[[MedallionSettings, object], Awaitable[SweepReport]]


def build_sweep_router(binding_name: str, sweep: Sweep) -> APIRouter:
    """The cron door at ``/<binding_name>``: a factory, because the path is the binding name known only at wiring."""
    router = APIRouter(tags=["run-plans"])

    async def _tick(request: Request, settings: SettingsDep, _: Annotated[None, Depends(require_dapr_token)]) -> SweepReport:
        return await sweep(settings, request.app.state.dapr)

    async def _ack_binding() -> dict[str, str]:
        """The sidecar probes with OPTIONS before delivering; a POST-only door is reported unroutable."""
        return {"status": "ok"}

    router.add_api_route(f"/{binding_name}", _tick, methods=["POST"])
    router.add_api_route(f"/{binding_name}", _ack_binding, methods=["OPTIONS"], include_in_schema=False)
    return router
