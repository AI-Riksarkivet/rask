"""The reconcile pass is REACHABLE — the half that `test_reconcile_report.py` cannot prove.

That suite drives `reconcile()` directly, so it stayed green for a module nothing could invoke: no cron
binding delivered to it, no OpenFGA client on the service, and therefore four of its seven categories
permanently reporting UNAVAILABLE in production while every unit test passed. These tests pin the WIRING
instead of the logic — the route exists at the binding name the sidecar posts to, it is token-gated, its
clients come from the app state, and a missing client degrades rather than fails.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from maintenance.api import routes
from maintenance.core.config import MaintenanceSettings
from pydantic import SecretStr


def _settings(**over: Any) -> MaintenanceSettings:
    return MaintenanceSettings(s3_bucket="unit-bucket", s3_secret_access_key=SecretStr("unit"), **over)


# --------------------------------------------------------------------------- #
# the binding surface — what the Dapr sidecar actually posts to
# --------------------------------------------------------------------------- #


def test_the_reconcile_route_exists_at_its_binding_name() -> None:
    """Dapr delivers a cron tick to `/<binding-name>` and nowhere else, so the route path IS the
    contract with the component. A mismatch is invisible in tests and silent in production — the
    sidecar posts, gets a 404, and the report simply never runs."""
    # The ROUTER's own routes, not the app's: this FastAPI resolves `include_router` lazily
    # (`_IncludedRouter`), so an app inspected before startup reports no expanded routes at all.
    declared = {(getattr(r, "path", ""), m) for r in routes.router.routes for m in getattr(r, "methods", set())}
    binding = f"/{_settings().reconcile_binding_name}"
    assert (binding, "POST") in declared, f"no POST route at {binding} — the sidecar's tick would 404"


def test_the_reconcile_binding_answers_the_options_preflight() -> None:
    """Dapr's binding-discovery pre-flight is an OPTIONS. Without a handler it 405s and Dapr logs the
    app as NOT consuming the binding — the cron then never fires and nothing reports why."""
    binding = f"/{_settings().reconcile_binding_name}"
    options = [r for r in routes.router.routes if getattr(r, "path", None) == binding and "OPTIONS" in getattr(r, "methods", set())]
    assert options, "no OPTIONS handler — Dapr would consider the binding unconsumed"


def test_the_sweep_and_the_reconcile_are_separate_bindings() -> None:
    """One binding for both would force the cheap read-only drift report onto the expensive
    data-rewriting sweep's cadence. Separate names, asserted so a later 'simplification' that merges
    them has to argue with a test."""
    settings = _settings()
    assert settings.binding_name != settings.reconcile_binding_name


def test_the_reconcile_route_is_token_gated() -> None:
    """The pass reads EVERY tenant's registry records and tuple counts. It mutates nothing, which is
    exactly why the gate is easy to forget — but an ungated trigger is an estate-wide disclosure, not
    merely a wasted scan."""
    binding = f"/{_settings().reconcile_binding_name}"
    post = cast(Any, next(r for r in routes.router.routes if getattr(r, "path", None) == binding and "POST" in getattr(r, "methods", set())))
    names = {getattr(d.call, "__name__", "") for d in post.dependant.dependencies}
    assert "require_dapr_token" in names, f"the reconcile binding is UNGATED (deps: {names})"


# --------------------------------------------------------------------------- #
# the clients — optional by design, so a missing one degrades rather than fails
# --------------------------------------------------------------------------- #


def test_a_missing_fga_client_degrades_the_report_instead_of_failing_it() -> None:
    """FGA down/unwired must cost the four authz categories, not the whole report — the other three
    still answer. A drift report that 500s because one of three stores is unreachable tells you
    nothing about the other two."""
    from maintenance.api import dependencies

    request = cast(Any, type("R", (), {"app": type("A", (), {"state": type("S", (), {})()})()})())
    assert dependencies.get_fga_client(request) is None  # absent attribute → None, never AttributeError
    assert dependencies.get_s3_client(request) is None


def test_the_route_hands_the_app_state_clients_to_the_reconciler(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring itself: whatever the lifespan put on `app.state` is what the pass reads three stores
    with. Asserted because the failure mode is silent — a route that quietly passes `None` produces a
    report that is entirely 'unavailable' and still exits 200."""
    seen: dict[str, Any] = {}

    async def fake_reconcile(settings: Any, client: Any = None, **kw: Any) -> Any:
        seen["client"] = client
        seen["bucket_client"] = kw.get("bucket_client")
        seen["warehouses_enabled"] = kw.get("warehouses_enabled")
        seen["control_root"] = kw.get("control_root")
        seen["fga_root_object"] = kw.get("fga_root_object")

        class _R:
            total = 0
            counts: dict[str, int] = {}
            unavailable: list[Any] = []
            skipped: list[Any] = []
            incomplete: list[Any] = []

            def model_dump(self, **_kw: Any) -> dict[str, Any]:
                return {"total": 0}

        return _R()

    monkeypatch.setattr(routes, "reconcile", fake_reconcile)
    settings = _settings(warehouses_enabled=True, fga_root_object="warehouse:custom")
    out = asyncio.run(routes.on_reconcile_cron(settings, "FGA-SENTINEL", "S3-SENTINEL"))

    assert out == {"total": 0}
    assert seen["client"] == "FGA-SENTINEL"
    assert seen["bucket_client"] == "S3-SENTINEL"
    # These mirror CATALOG settings and are passed IN, never inferred: a divergent fga_root_object
    # reports the estate root as a ghost forever, and a divergent warehouses_enabled reports every
    # namespace as unbound.
    assert seen["warehouses_enabled"] is True
    assert seen["fga_root_object"] == "warehouse:custom"
    assert seen["control_root"] == settings.resolved_control_root


def test_an_overlapping_tick_skips_rather_than_stacking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-only work cannot corrupt anything by overlapping, but it can spend three stores' read budget
    twice and emit two reports a human must reconcile. Skip, never queue — the next tick re-reads
    current state anyway, so a queued one would only replay stale work."""

    async def never_called(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("the second tick ran a reconcile while one was already in flight")

    async def drive() -> dict[str, Any]:
        async with routes._reconcile_lock:
            monkeypatch.setattr(routes, "reconcile", never_called)
            return await routes.on_reconcile_cron(_settings(), None, None)

    out = asyncio.run(drive())
    assert out["status"] == "skipped"


def test_the_service_never_provisions_an_fga_store() -> None:
    """A maintenance job that provisioned its own store would read an EMPTY one and then report every
    real tenant as a ghost — a false report is worse than an absent one. With the ids unpinned the
    client is None (→ unavailable), and `fga.provision` is never referenced by this service at all."""
    from pathlib import Path

    from maintenance import service

    assert service._make_fga_client(_settings(fga_enabled=True)) is None  # ids unset → None, not a new store
    assert service._make_fga_client(_settings(fga_enabled=False)) is None
    # `fga.provision(...)` is the call that would create a store; prose about NOT provisioning is fine
    # and is in fact the point, so match the CALL, not the word.
    src = Path(service.__file__).read_text()
    assert "fga.provision" not in src, "the maintenance service must never provision an FGA store"


def test_the_reconcile_client_is_read_only_by_construction() -> None:
    """The service holds no tuple-WRITE path: the reconciler is report-only, and the cheapest way to
    keep it that way is for the write verbs to be absent from the module entirely."""
    from pathlib import Path

    from maintenance.services import reconcile as mod

    src = Path(mod.__file__).read_text()
    for verb in ("write_tuples", "delete_tuples", "revoke_object_tuples", "grant_on_create"):
        assert verb not in src, f"the reconciler references {verb} — it must only READ"


def test_the_app_registers_both_bindings_together() -> None:
    """Both cron routes come from the one router the service includes, so a service that boots has both
    or neither. Guards the split-brain where the sweep ticks and the reconcile silently does not."""
    settings = _settings()
    posted = {getattr(r, "path", "") for r in routes.router.routes if "POST" in getattr(r, "methods", set())}
    assert f"/{settings.binding_name}" in posted
    assert f"/{settings.reconcile_binding_name}" in posted


def test_the_reconcile_route_is_reachable_over_http() -> None:
    """End of the wiring chain: a POST to the binding path reaches the handler through a real app.

    The token gate is overridden here (that is proven separately, above) so this asserts REACHABILITY —
    the thing that was missing and that no amount of unit-testing `reconcile()` could have caught."""
    from service_kit.governed.dapr_auth import require_dapr_token

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_dapr_token] = lambda: None

    async def stub(*_a: Any, **_kw: Any) -> Any:
        class _R:
            total = 0
            counts: dict[str, int] = {}
            unavailable: list[Any] = []
            skipped: list[Any] = []
            incomplete: list[Any] = []

            def model_dump(self, **_kw: Any) -> dict[str, Any]:
                return {"total": 0, "counts": {}}

        return _R()

    original = routes.reconcile
    routes.reconcile = cast(Any, stub)
    try:
        with TestClient(app) as client:
            response = client.post(f"/{_settings().reconcile_binding_name}")
        assert response.status_code == 200, response.text
        assert response.json() == {"total": 0, "counts": {}}
    finally:
        routes.reconcile = cast(Any, original)
