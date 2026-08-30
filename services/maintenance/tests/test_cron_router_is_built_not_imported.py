"""The two cron routes must be BUILT from settings, not stamped into the module at import (MAINT-13).

`routes.py` read `get_settings()` at module scope and called `router.add_api_route` at module scope, so
the binding names — the whole point of `MAINTENANCE_BINDING_NAME` / `MAINTENANCE_RECONCILE_BINDING_NAME`
— were frozen by whatever environment happened to be present when the first import ran. Nothing could
drive them from a test, and an app assembled with different settings than the ones the module cached
would serve paths the sidecar never POSTs to. The tags rode the same drift: declared per route, five
lines apart, instead of once on the router that owns both.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.routing import APIRoute
from pydantic import SecretStr

from maintenance.api.routes import build_router
from maintenance.core.config import MaintenanceSettings


def _settings(binding: str, reconcile: str) -> MaintenanceSettings:
    return MaintenanceSettings(
        s3_bucket="lance-catalog",
        s3_secret_access_key=SecretStr("unit"),
        binding_name=binding,
        reconcile_binding_name=reconcile,
    )


def _paths(router: APIRouter) -> set[str]:
    return {route.path for route in router.routes if isinstance(route, APIRoute)}


def test_the_binding_names_come_from_the_settings_the_router_is_given() -> None:
    alpha = build_router(_settings("alpha-cron", "alpha-reconcile"))
    beta = build_router(_settings("beta-cron", "beta-reconcile"))

    assert _paths(alpha) == {"/alpha-cron", "/alpha-reconcile"}
    assert _paths(beta) == {"/beta-cron", "/beta-reconcile"}


def test_both_bindings_keep_their_POST_and_their_OPTIONS_ack() -> None:
    """Dapr's discovery pre-flight is an OPTIONS on the same path — without it the app is logged as
    not consuming the binding, and the cron never fires at all."""
    router = build_router(_settings("sweep-cron", "reconcile-cron"))
    methods = {(route.path, tuple(sorted(route.methods or set()))) for route in router.routes if isinstance(route, APIRoute)}
    assert methods == {
        ("/sweep-cron", ("POST",)),
        ("/sweep-cron", ("OPTIONS",)),
        ("/reconcile-cron", ("POST",)),
        ("/reconcile-cron", ("OPTIONS",)),
    }


def test_every_triggering_POST_keeps_the_sidecar_token_gate() -> None:
    """The gate is what makes the sweep and the estate-wide registry read sidecar-only."""
    router = build_router(_settings("sweep-cron", "reconcile-cron"))
    posts = [route for route in router.routes if isinstance(route, APIRoute) and "POST" in (route.methods or set())]
    assert len(posts) == 2
    for route in posts:
        names = [getattr(dep.call, "__name__", "") for dep in route.dependant.dependencies]
        assert "require_dapr_token" in names, f"{route.path} lost its sidecar token gate"


def test_the_tag_is_declared_on_the_router_not_repeated_per_route() -> None:
    assert build_router(_settings("a", "b")).tags == ["maintenance"]
