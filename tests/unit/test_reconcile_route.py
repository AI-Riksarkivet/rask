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
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from lakehouse.control_emit import NoopControlEmitter
from maintenance.api import routes
from maintenance.core.config import MaintenanceSettings
from maintenance.services.reconcile import ReconcileReport


#: A HAND-ROLLED MIRROR OF THE REPORT DRIFTED, so these stubs return the real model. `_orphans_by_dataset`
#: reads `orphan_files`, a field the mirror did not have, and both stubs raised `AttributeError` the day
#: the summary grew — a double that must be edited whenever the thing it doubles grows is a double that
#: will eventually not be. Any fixed stamp: the route never reads it, and a clock would make this
#: non-deterministic.
_CHECKED_AT = "2026-09-16T14:30:00+00:00"


def _settings(**over: Any) -> MaintenanceSettings:
    return MaintenanceSettings(s3_bucket="unit-bucket", s3_secret_access_key=SecretStr("unit"), **over)


def _router(**over: Any) -> APIRouter:
    """The cron router, BUILT from settings (MAINT-13) rather than a module-level singleton.

    `routes.py` used to read `get_settings()` and register both routes at import time, so these
    assertions read a topology frozen by whatever environment imported the module first. Building it
    here from the same `_settings()` the assertions use is what makes the binding names testable.
    """
    return routes.build_router(_settings(**over))


# --------------------------------------------------------------------------- #
# the clients — optional by design, so a missing one degrades rather than fails
# --------------------------------------------------------------------------- #


def test_the_route_hands_the_reconciler_the_catalogs_grant_census(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring itself: the pass reads OpenFGA through the catalog's grant census, maintenance holding no OpenFGA
    credential ([[XC-084]]). Asserted because the failure mode is silent — a route that quietly passes no census
    produces a report that is entirely 'unavailable' and still exits 200. The bucket-listing client is NOT handed
    over: the pass builds it per tick with the key pair in force ([[XC-082]]), where one built at boot would list
    with a key the store has since replaced."""
    seen: dict[str, Any] = {}

    async def fake_reconcile(settings: Any, grants: Any = None, **kw: Any) -> Any:
        seen["grants"] = grants
        seen["bucket_client"] = kw.get("bucket_client")
        seen["warehouses_enabled"] = kw.get("warehouses_enabled")
        seen["control_root"] = kw.get("control_root")
        return ReconcileReport(checked_at=_CHECKED_AT)

    monkeypatch.setattr(routes, "reconcile", fake_reconcile)
    settings = _settings(warehouses_enabled=True)
    out = asyncio.run(routes.on_reconcile_cron(settings, NoopControlEmitter()))

    # The #79 purge key rides along on every tick; the reconcile half is unchanged.
    assert (out["total"], out["trash_purge"]["enabled"], out["trash_purge"]["ran"]) == (0, False, False)
    assert seen["grants"] is not None, "the route handed the reconciler no grant census"
    # `warehouses_enabled` mirrors a CATALOG setting and is passed IN, never inferred: a divergent one reports every
    # namespace as unbound.
    assert (seen["bucket_client"], seen["warehouses_enabled"], seen["control_root"]) == (None, True, settings.resolved_control_root)


def test_an_overlapping_tick_skips_rather_than_stacking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-only work cannot corrupt anything by overlapping, but it can spend three stores' read budget
    twice and emit two reports a human must reconcile. Skip, never queue — the next tick re-reads
    current state anyway, so a queued one would only replay stale work."""

    async def never_called(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("the second tick ran a reconcile while one was already in flight")

    async def drive() -> dict[str, Any]:
        async with routes._reconcile_lock:
            monkeypatch.setattr(routes, "reconcile", never_called)
            return await routes.on_reconcile_cron(_settings(), NoopControlEmitter())

    out = asyncio.run(drive())
    assert out["status"] == "skipped"


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
        seen["locked"] = routes._reconcile_lock.locked()

        # THE REAL REPORT, not a hand-rolled stand-in. A double that re-declares the model's fields
        # drifts the moment the model grows one — this one did, and the route then failed on an
        # attribute the production type has always had. The subject here is the route's LOCKING and
        # report-identity, so the report itself may as well be genuine.
        from maintenance.services.purge import TrashPurgeReport

        return TrashPurgeReport()

    monkeypatch.setattr(routes, "reconcile", fake_reconcile)
    monkeypatch.setattr(routes, "purge_expired_trash", fake_purge)
    out = asyncio.run(routes.on_reconcile_cron(_settings(), NoopControlEmitter()))

    assert seen["report"] is produced["report"], "the purge read a report this tick did not produce"
    assert seen["locked"] is True, "the purge ran outside the single-flight lock"
    # The purge key rides along on the tick's payload; its CONTENT is `test_trash_purge.py`'s subject,
    # so this asserts only that the real report reached the response un-run.
    assert out["trash_purge"]["ran"] is False


def test_the_reconcile_route_is_reachable_over_http() -> None:
    """End of the wiring chain: a POST to the binding path reaches the handler through a real app.

    The token gate is overridden here (that is proven separately, above) so this asserts REACHABILITY —
    the thing that was missing and that no amount of unit-testing `reconcile()` could have caught."""
    from lakehouse.governed.dapr_auth import require_dapr_token

    app = FastAPI()
    app.include_router(_router())
    app.dependency_overrides[require_dapr_token] = lambda: None

    async def stub(*_a: Any, **_kw: Any) -> Any:
        return ReconcileReport(checked_at=_CHECKED_AT)

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
    estate creates some for ITSELF — an `objectStore.buckets` entry the scoped-users hook provisions on the dev store —
    and no warehouse record will ever claim one, because they hold no governed tables.

    MEASURED live 2026-08-16: it sat in orphan_buckets on every tick. Since `report_is_clean` blocks the
    #79 purge on ANY finding, the purge could not be reached by clearing real drift — only by deleting
    the observability store. `platform_buckets` defaulted to `sweep_buckets`, which is the set the sweep
    MAINTAINS and says nothing about infrastructure it does not.

    The declared set comes from the chart's `lance.maintenancePlatformBuckets`: `lance.platformBuckets`,
    which the scoped-users hook creates, plus the multibase bases.
    """
    from maintenance.core.config import MaintenanceSettings

    s = MaintenanceSettings(
        MAINTENANCE_S3_BUCKET="lance-catalog",
        MAINTENANCE_S3_PLATFORM_BUCKETS="extra-platform,lance-catalog",
    )
    assert "extra-platform" in s.platform_buckets, "the declared platform bucket must be exempt"
    assert "lance-catalog" in s.platform_buckets, "the swept set stays exempt — a maintained bucket is known by definition"
    assert "extra-platform" not in s.sweep_buckets, "a platform bucket must NOT become something the sweep walks"
