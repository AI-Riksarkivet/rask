"""Mounting a Dapr cron tick on the catalog: the route a ``bindings.cron`` Component delivers to.

Dapr delivers an input binding to ``POST /<component name>`` at the pod ROOT, never under ``RASK_API_PREFIX``, so the
Component's name, the setting that names it to the catalog and the served path are one string. A tick is mounted only
when the chart names its binding: an unnamed binding means a deployment with no Component behind the route.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel


async def _ack_binding() -> dict[str, str]:
    """Dapr's startup pre-flight (``OPTIONS /<binding name>``): a 2xx confirms this app consumes the binding."""
    return {"status": "ok"}


def mount_cron(app: FastAPI, binding_name: str, tick: Callable[..., Awaitable[BaseModel]], *, tag: str) -> bool:
    """Mount ``tick`` at ``POST /<binding_name>`` with its pre-flight when a binding is named; report whether it was."""
    if not binding_name:
        return False
    router = APIRouter()
    router.add_api_route(f"/{binding_name}", tick, methods=["POST"], tags=[tag])
    router.add_api_route(f"/{binding_name}", _ack_binding, methods=["OPTIONS"], include_in_schema=False)
    app.include_router(router)
    return True
