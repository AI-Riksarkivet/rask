"""SK-09 — the lifecycle invariants `/readyz` reads are the FACTORY's job, not a convention.

`service_kit.probes.readyz` answers off two flags on ``app.state``: ``startup_complete`` and
``shutting_down``. Nothing set them for an app built by ``make_service_app`` — every service that
had them set them by hand in its own lifespan, and the five factory-built deployables (compute,
controlplane, ingest, flows, notifications) do not. ``compute``'s lifespan even logs the STRING
``"startup_complete"`` while never setting the attribute, which is how the miss survived review.

The consequence is a shipped one, not a stylistic one: `/readyz` on those five reports ``starting``
for the whole life of the pod, so a readiness gate over it never opens, and ``shutting_down`` never
flips, so the drain never begins.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from service_kit import make_service_app
from service_kit.config import Settings
from service_kit.lifecycle import is_draining, is_started


def _bare_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """A lifespan exactly like compute's: it sets `settings` and nothing else."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        yield

    return lifespan


def _app() -> FastAPI:
    return make_service_app(title="probe-subject", routers=[APIRouter()], lifespan=_bare_lifespan)


def test_readyz_is_ready_once_the_lifespan_has_entered() -> None:
    app = _app()
    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 200, f"a started app still reports {response.json()!r}"
        assert response.json()["status"] == "ready"


def test_the_flags_are_set_by_the_factory_not_by_the_service() -> None:
    app = _app()
    with TestClient(app):
        assert is_started(app) is True
        assert is_draining(app) is False


def test_the_drain_flag_flips_when_the_lifespan_unwinds() -> None:
    app = _app()
    with TestClient(app):
        pass
    assert is_draining(app) is True, "the app never entered the drain state, so /readyz would answer 'ready' while dying"
    assert is_started(app) is False, "a stopped app must not still claim it is started"
