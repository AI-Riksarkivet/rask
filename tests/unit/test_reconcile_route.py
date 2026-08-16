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

from service_kit.control_emit import NoopControlEmitter


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
    out = asyncio.run(routes.on_reconcile_cron(settings, "FGA-SENTINEL", "S3-SENTINEL", NoopControlEmitter()))

    # The #79 purge key rides along on every tick; the reconcile half is unchanged.
    assert out["total"] == 0
    assert out["trash_purge"]["enabled"] is False and out["trash_purge"]["ran"] is False
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
            return await routes.on_reconcile_cron(_settings(), None, None, NoopControlEmitter())

    out = asyncio.run(drive())
    assert out["status"] == "skipped"


def test_the_service_never_provisions_an_fga_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """A maintenance job that provisioned its own store would read an EMPTY one and then report every
    real tenant as a ghost — a false report is worse than an absent one.

    The guard is the absent CALL, not a None return. This test used to assert that unpinned yields
    ``None`` and treated that as proof of not-provisioning, which conflated two different claims and
    pinned a real bug in place: see the sibling test for what unpinned must actually do.
    """
    from pathlib import Path

    from maintenance import service

    async def _no_store(*_a: Any, **_kw: Any) -> Any:
        return None

    monkeypatch.setattr(service.fga, "resolve", _no_store)
    assert asyncio.run(service._make_fga_client(_settings(fga_enabled=False))) is None
    assert asyncio.run(service._make_fga_client(_settings(fga_enabled=True))) is None, "no store to resolve → still None, never a new one"

    # The CALL is what creates a store; prose about not provisioning is fine and is in fact the point.
    # Matched as `fga.provision(` — the bare name matched documentation too, so a docstring explaining
    # WHY the service must not provision failed the test asserting that it does not.
    src = Path(service.__file__).read_text()
    assert "fga.provision(" not in src, "the maintenance service must never provision an FGA store"


def test_an_UNPINNED_store_is_RESOLVED_by_name_rather_than_abandoned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading is not authoring — the distinction ingest already paid for once.

    The chart's DEFAULT posture is `auth.fgaStoreId: ""`, because a store id is a per-cluster ULID that
    cannot be a committed default. Unpinned used to return ``None`` outright, so in every default
    deployment this service had no FGA client at all: `ghost_projects`, `ghost_warehouses` and
    `unreferenced_projects` reported UNAVAILABLE on every tick (measured live 2026-08-16), and because
    `report_is_clean` blocks on ANY unavailable category, the #79 expired-trash purge could never
    certify anywhere.

    `fga.resolve` is read-only: it cannot create a store or write a model, and answers ``None`` when the
    estate is not bootstrapped. So this still fails closed — it just stops failing closed against an
    estate that is sitting right there. Same fix, same reasoning, as `ingest.__init__`'s.
    """
    from maintenance import service

    seen: list[str] = []

    async def _resolve(api_url: str, **_kw: Any) -> tuple[str, str]:
        seen.append(api_url)
        return ("store-01ABC", "model-01XYZ")

    made: list[tuple[str, str, str]] = []
    monkeypatch.setattr(service.fga, "resolve", _resolve)
    monkeypatch.setattr(service.fga, "make_client", lambda url, store, model, **_kw: made.append((url, store, model)) or "CLIENT")

    client = asyncio.run(service._make_fga_client(_settings(fga_enabled=True)))

    assert client == "CLIENT", "an unpinned service with a resolvable store must get a client, not None"
    assert seen, "resolve was never attempted — unpinned fell straight through to None again"
    assert made and made[0][1:] == ("store-01ABC", "model-01XYZ"), f"the client must be built from the RESOLVED ids, got {made}"


def test_a_resolve_that_raises_degrades_rather_than_killing_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenFGA being slow to accept connections is an ordering blip, not a reason to CrashLoopBackOff.

    The sweep is this service's primary job and needs no FGA at all, so an unreachable authz endpoint
    must cost the authz CATEGORIES and nothing else.
    """
    from maintenance import service

    async def _boom(*_a: Any, **_kw: Any) -> Any:
        raise ConnectionRefusedError("openfga not up yet")

    monkeypatch.setattr(service.fga, "resolve", _boom)
    assert asyncio.run(service._make_fga_client(_settings(fga_enabled=True))) is None


def test_the_reconcile_client_is_read_only_by_construction() -> None:
    """The RECONCILER holds no tuple-write path, and the cheapest way to keep it that way is for the
    write verbs to be absent from the module entirely.

    Scoped to `reconcile.py`, not the service: since #79 the sibling `purge.py` DOES revoke (that is the
    first step of destroying an expired trash record — grants must never outlive the bytes). The claim
    that survives is narrower and more useful: the module that decides whether the estate is clean
    cannot itself change the estate.
    """
    from pathlib import Path

    from maintenance.services import reconcile as mod

    src = Path(mod.__file__).read_text()
    for verb in ("write_tuples", "delete_tuples", "revoke_object_tuples", "grant_on_create"):
        assert verb not in src, f"the reconciler references {verb} — it must only READ"


def test_the_service_can_never_GRANT_a_tuple() -> None:
    """The purge revokes; nothing in this service grants.

    A maintenance job that could write a grant would be an unaudited privilege door on a component whose
    whole justification is that it only cleans up. Asserted across the WHOLE service package rather than
    one module, because the risk is someone adding the capability somewhere convenient.

    Matched against the AST's CALLED names, never the source text: the docstrings here necessarily name
    these verbs to explain which ones are off-limits, and a substring gate that fires on its own
    explanation is a gate people delete.
    """
    import ast
    from pathlib import Path

    from maintenance import service as svc

    forbidden = {"write_tuples", "grant_on_create", "seed_ownership", "provision"}
    for path in sorted(Path(svc.__file__).parent.rglob("*.py")):
        called = {
            node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
        }
        offenders = sorted(called & forbidden)
        assert offenders == [], f"{path.name} calls {offenders} — maintenance may revoke, never grant"


def test_the_purge_consumes_THIS_ticks_report_inside_the_same_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is "the drift report ran clean", so the object it reads must be the one just produced.

    A purge fed a stored or previous report certifies a state that no longer exists — the estate could
    have drifted in between, and the whole permission model rests on the two being the same tick. Pinned
    by identity, which is the only way to state it that a later refactor cannot quietly weaken.
    """
    produced: dict[str, Any] = {}
    seen: dict[str, Any] = {}

    async def fake_reconcile(*_a: Any, **_kw: Any) -> Any:
        from maintenance.services.reconcile import ReconcileReport

        produced["report"] = ReconcileReport(checked_at="t")
        return produced["report"]

    async def fake_purge(_settings: Any, **kw: Any) -> Any:
        seen["report"] = kw["report"]
        seen["fga_client"] = kw["fga_client"]
        seen["locked"] = routes._reconcile_lock.locked()

        class _P:
            def model_dump(self, **_k: Any) -> dict[str, Any]:
                return {"ran": False}

            purged: list[Any] = []
            refused: list[Any] = []
            capped = 0

        return _P()

    monkeypatch.setattr(routes, "reconcile", fake_reconcile)
    monkeypatch.setattr(routes, "purge_expired_trash", fake_purge)
    out = asyncio.run(routes.on_reconcile_cron(_settings(), "FGA-SENTINEL", None, NoopControlEmitter()))

    assert seen["report"] is produced["report"], "the purge read a report this tick did not produce"
    assert seen["fga_client"] == "FGA-SENTINEL", "the purge got no FGA client — it could not revoke"
    assert seen["locked"] is True, "the purge ran outside the single-flight lock"
    assert out["trash_purge"] == {"ran": False}


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
        body = response.json()
        assert {"total": body["total"], "counts": body["counts"]} == {"total": 0, "counts": {}}
        # The #79 purge answers on the same tick, and OFF is what the shipped configuration reports.
        assert body["trash_purge"]["ran"] is False
    finally:
        routes.reconcile = cast(Any, original)


def test_a_PLATFORM_bucket_is_never_reported_as_an_orphan() -> None:
    """A finding no operator can action keeps the drift report permanently non-clean.

    `orphan_buckets` reports buckets that exist in storage and that no warehouse record claims. The
    estate creates some for ITSELF — `rask-observability` is the RustFS bucket this chart's own
    rustfs-mkbucket Job provisions for GreptimeDB's object store — and no warehouse record will ever
    claim one, because they hold no governed tables.

    MEASURED live 2026-08-16: it sat in orphan_buckets on every tick. Since `report_is_clean` blocks the
    #79 purge on ANY finding, the purge could not be reached by clearing real drift — only by deleting
    the observability store. `platform_buckets` defaulted to `sweep_buckets`, which is the set the sweep
    MAINTAINS and says nothing about infrastructure it does not.

    The declared set comes from `rustfs.buckets`, the same values key the mkbucket Job verifies, so the
    exemption cannot drift away from what is actually provisioned.
    """
    from maintenance.core.config import MaintenanceSettings

    s = MaintenanceSettings(
        MAINTENANCE_S3_BUCKET="lance-catalog",
        MAINTENANCE_S3_PLATFORM_BUCKETS="rask-observability,lance-catalog",
    )
    assert "rask-observability" in s.platform_buckets, "the declared platform bucket must be exempt"
    assert "lance-catalog" in s.platform_buckets, "the swept set stays exempt — a maintained bucket is known by definition"
    assert "rask-observability" not in s.sweep_buckets, "a platform bucket must NOT become something the sweep walks"


def test_platform_buckets_defaults_to_the_swept_set_when_undeclared() -> None:
    """No declaration must not widen the exemption — an undeclared estate keeps the old behaviour."""
    from maintenance.core.config import MaintenanceSettings

    s = MaintenanceSettings(MAINTENANCE_S3_BUCKET="lance-catalog")
    assert s.platform_buckets == s.sweep_buckets
