"""The prune route's guard is real only when the app holds the token, and the app must refuse to boot without it.

`require_dapr_token` compares the sidecar's `dapr-api-token` header against the app's `APP_API_TOKEN`
and, by its own docstring, is a no-op when that variable is unset — "the open dev default", made safe
because `assert_app_token_configured` "makes that a startup error once Dapr ingest is actually
enabled, so the no-op can only apply in dev." Every sibling that hosts a sidecar-delivered route calls
it at boot (catalog, lineage, maintenance, both medallion apps). `compute` did not.

Measured on the deployed estate, 2026-09-02: the compute pod's Dapr SIDECAR carried `APP_API_TOKEN`
(so it stamps every delivery) while the APP container carried none — the chart renders the variable
only for `daprIngest`/`lanceWriter` services and compute was neither — so `POST /compute-prune-jobs-cron`
was reachable, unsigned, from any pod in the namespace, and each call deletes terminal Ray jobs. The
guard read as present and enforced nothing.

Two things are pinned, and they are different claims:

1. **With the token configured, the guard is real** — an unsigned POST is refused, a signed one is
   admitted. This is what "the route is sidecar-only" actually means.
2. **With Dapr on and no token, the process does not start.** Failing loudly at boot is the estate's
   rule for a missing security variable; the alternative is a healthy-looking pod whose destructive
   route is open, which is precisely what shipped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """`get_compute_settings` is `lru_cache`d; every case here changes the environment it reads."""
    from compute import dependencies

    dependencies.get_compute_settings.cache_clear()
    monkeypatch.setenv("RAY_DASHBOARD_URL", "http://127.0.0.1:9")  # refused fast: no Ray in these tests
    yield
    dependencies.get_compute_settings.cache_clear()


def _boot() -> None:
    """Enter compute's lifespan once, exactly as uvicorn would."""
    from compute.lifespan import make_lifespan
    from service_kit import build_settings

    app = FastAPI()
    lifespan = make_lifespan(build_settings())

    async def _drive() -> None:
        async with lifespan(cast(Any, app)):
            pass

    asyncio.run(_drive())


def test_dapr_on_with_no_token_refuses_to_boot(monkeypatch: pytest.MonkeyPatch, fresh_settings: None) -> None:
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="APP_API_TOKEN"):
        _boot()


def test_dapr_on_with_a_token_boots(monkeypatch: pytest.MonkeyPatch, fresh_settings: None) -> None:
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.setenv("APP_API_TOKEN", "sidecar-secret")
    _boot()


def test_dapr_off_boots_without_a_token(monkeypatch: pytest.MonkeyPatch, fresh_settings: None) -> None:
    """The dev loop: no sidecar, no token, no refusal — the guard is a documented no-op there."""
    monkeypatch.setenv("RASK_DAPR_ENABLED", "false")
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    _boot()


@pytest.fixture
def signed_app(monkeypatch: pytest.MonkeyPatch, fresh_settings: None) -> Iterator[TestClient]:
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.setenv("APP_API_TOKEN", "sidecar-secret")
    monkeypatch.setenv("RASK_API_PREFIX", "/api/v1")
    from compute import app

    with TestClient(app) as client:
        yield client


def test_an_unsigned_prune_post_is_refused_when_the_token_is_configured(signed_app: TestClient) -> None:
    resp = signed_app.post("/compute-prune-jobs-cron")
    assert resp.status_code == 403, f"an unsigned POST reached the prune route: {resp.status_code} {resp.text[:120]}"


def test_a_wrongly_signed_prune_post_is_refused(signed_app: TestClient) -> None:
    resp = signed_app.post("/compute-prune-jobs-cron", headers={"dapr-api-token": "not-the-secret"})
    assert resp.status_code == 403


def test_a_correctly_signed_prune_post_is_admitted(signed_app: TestClient) -> None:
    """Admitted means it reaches the handler — which, with no Ray, reports the tick as skipped."""
    resp = signed_app.post("/compute-prune-jobs-cron", headers={"dapr-api-token": "sidecar-secret"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 0
