"""A lakehouse app answers `/livez` whatever its lifecycle, and `/readyz` by it before its own check is asked.

`build_lance_service_app` mounts `lakehouse.probes` at the root of catalog, lineage and maintenance. Liveness only
proves the event loop answers, so a pod that has not started or is draining stays alive and is not restarted. Readiness
reads the lifecycle flags the service's lifespan sets (`lakehouse.lifecycle`): until the lifespan has started the
answer is `starting`, once it drains it is `shutting_down`, both 503 so the pod leaves the Service, and only between
the two is the service's `ready_check` asked. The check here answers `ready`, so a gate that let it through would
read 200.

Between the two, a service answers from its dependencies' verdicts (`lakehouse.health`): one it rides through not
answering keeps it in rotation, `degraded` (200), and one it cannot serve without takes it out, `unhealthy` (503).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from lakehouse.health import Dependency, HealthWatch
from lakehouse.lance_app import build_lance_service_app
from lakehouse.lifecycle import mark_draining, mark_started
from lakehouse.schemas.health import Readiness, ReadinessStatus


async def _ready(_request: Request) -> Readiness:
    return Readiness(status=ReadinessStatus.ready)


def _unstarted(_app: FastAPI) -> None:
    """A lifespan that has not reached the point where the service marks itself started."""


def _draining(app: FastAPI) -> None:
    mark_started(app)
    mark_draining(app)


@pytest.mark.parametrize(
    ("lifecycle", "readiness"),
    [
        pytest.param(_unstarted, "starting", id="not-yet-started"),
        pytest.param(_draining, "shutting_down", id="draining"),
    ],
)
def test_a_lakehouse_app_is_alive_and_not_ready_until_it_has_started_and_while_it_drains(lifecycle: Callable[[FastAPI], None], readiness: str) -> None:
    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
        lifecycle(app)
        yield

    app = build_lance_service_app(
        title="t",
        docs_enabled=False,
        audit_enabled=False,
        lifespan=_lifespan,
        log=logging.getLogger("t"),
        ready_check=_ready,
        max_body_bytes=1024,
        request_deadline_seconds=30.0,
    )
    with TestClient(app) as client:
        alive = client.get("/livez")
        ready = client.get("/readyz")

    assert (alive.status_code, alive.json()) == (200, {"status": "ok"})
    assert (ready.status_code, ready.json()) == (503, {"status": readiness, "components": {}})


async def _answers() -> None:
    return None


async def _does_not_answer() -> None:
    raise ConnectionError("no answer")


@pytest.mark.parametrize(
    ("ridden_answers", "answered"),
    [
        pytest.param(True, {"status": "ready", "components": {"needed": "healthy", "ridden": "healthy"}}, id="every-dependency-answers"),
        pytest.param(False, {"status": "degraded", "components": {"needed": "healthy", "ridden": "unavailable"}}, id="one-it-rides-through-does-not"),
    ],
)
def test_a_started_lakehouse_app_stays_in_rotation_while_a_dependency_it_rides_through_does_not_answer(
    ridden_answers: bool, answered: dict[str, object]
) -> None:
    """The other side, one it cannot serve without not answering (503, `unhealthy`), is lineage's feed
    (`services/lineage/tests/test_readiness_never_waits_on_the_feed.py`)."""
    dependencies = [
        Dependency(name="needed", read=_answers, required=True),
        Dependency(name="ridden", read=_answers if ridden_answers else _does_not_answer, required=False),
    ]
    watch = HealthWatch(dependencies)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
        for dependency in dependencies:
            await watch.check(dependency)
        mark_started(app)
        yield

    async def _watched(_request: Request) -> Readiness:
        return watch.readiness()

    app = build_lance_service_app(
        title="t",
        docs_enabled=False,
        audit_enabled=False,
        lifespan=_lifespan,
        log=logging.getLogger("t"),
        ready_check=_watched,
        max_body_bytes=1024,
        request_deadline_seconds=30.0,
    )
    with TestClient(app) as client:
        ready = client.get("/readyz")

    assert (ready.status_code, ready.json()) == (200, answered)
