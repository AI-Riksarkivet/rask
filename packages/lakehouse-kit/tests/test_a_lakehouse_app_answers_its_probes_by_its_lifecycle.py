"""A lakehouse app answers `/livez` whatever its lifecycle, and `/readyz` by it before its own check is asked.

`build_lance_service_app` mounts `service_kit.probes` at the root of catalog, lineage and maintenance. Liveness only
proves the event loop answers, so a pod that has not started or is draining stays alive and is not restarted. Readiness
reads the lifecycle flags the service's lifespan sets (`service_kit.lifecycle`): until the lifespan has started the
answer is `starting`, once it drains it is `shutting_down`, both 503 so the pod leaves the Service, and only between
the two is the service's `ready_check` asked. The check here answers `ready`, so a gate that let it through would
read 200.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from lakehouse_kit.lance_app import build_lance_service_app
from service_kit.lifecycle import mark_draining, mark_started
from service_kit.schemas.health import Readiness, ReadinessStatus


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

    app = build_lance_service_app(title="t", docs_enabled=False, audit_enabled=False, lifespan=_lifespan, log=logging.getLogger("t"), ready_check=_ready)
    with TestClient(app) as client:
        alive = client.get("/livez")
        ready = client.get("/readyz")

    assert (alive.status_code, alive.json()) == (200, {"status": "ok"})
    assert (ready.status_code, ready.json()) == (503, {"status": readiness, "components": {}})
