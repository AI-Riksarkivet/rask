"""One 422 body across the fleet, installed once (open_python-audit X11).

X11 filed ingest as the odd one out: it called `install_problem_handlers` on top of an app
`make_service_app` had already built, so its `RequestValidationError` body came from the
Lance-Namespace translator while its three fleet siblings' came from `service_kit.exceptions` — two
shapes for one status, decided by which service you happened to call.

THE DIVERGENCE ITSELF IS ALREADY GONE, and it was closed the right way round: `make_service_app` now
calls `_install_ns_problem_handlers` for EVERY app it builds (`service_kit/app.py`, whose comment
names this finding), so all four speak the Lance envelope rather than ingest being pulled back to the
thinner one. This file pins that — a regression that dropped the factory's install would put the
fleet back to two shapes silently, because each app alone still answers a well-formed 422.

What was left over is ingest's own call, now a second registration of the same three handlers over
the top of the factory's. Dead, and dead code goes in the change that kills it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel


REPO = Path(__file__).resolve().parents[2]

#: Every app `make_service_app` builds. `flows` and `notifications` join ingest, compute and
#: controlplane; the gateway builds its own `FastAPI` and is a proxy, not a fleet API surface.
FLEET_APPS = ["ingest", "compute", "controlplane", "flows", "notifications"]


class _Body(BaseModel):
    n: int


@contextmanager
def _probe_route(app: FastAPI) -> Iterator[None]:
    """Add a probe route to a REAL, module-level app and take it away again.

    These apps are process-wide singletons, so a route added here and left behind changes what
    `tests/unit/test_openapi_contract.py` sees the live app serve — and that test then fails for a
    reason that has nothing to do with it. Testing the real app is the point, so the route goes on and
    comes off.
    """
    before = list(app.router.routes)
    schema = app.openapi_schema
    try:
        yield
    finally:
        app.router.routes[:] = before
        app.openapi_schema = schema


def _fleet_app(name: str) -> FastAPI:
    if name == "ingest":
        from ingest import create_app

        return create_app()
    module = __import__(name)
    app = module.app
    assert isinstance(app, FastAPI)
    return app


@pytest.mark.parametrize("name", FLEET_APPS)
def test_every_fleet_app_renders_one_validation_envelope(name: str) -> None:
    """A 422 is the status a client is most likely to hit, so its body must not depend on the service.

    The Lance envelope is the estate's answer: RFC 9457 fields PLUS the numeric spec `code` a
    generated client's `ErrorResponse` requires, PLUS the per-field `errors` list.
    """
    app = _fleet_app(name)
    with _probe_route(app):

        @app.post("/_probe_validation")
        async def _probe(_body: _Body) -> None:
            return None

        response = TestClient(app, raise_server_exceptions=False).post("/_probe_validation", json={"n": "not-an-int"})
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/problem+json"), response.headers
    body = response.json()
    assert body["title"] == "Validation Error", body
    assert body["code"] == 13, body  # ErrorCode.INVALID_INPUT — what a generated client dispatches on
    assert [entry["field"] for entry in body["errors"]] == ["body.n"], body


def test_no_service_re_installs_the_handlers_its_factory_already_carries() -> None:
    """X11's leftover: `make_service_app` installs the Lance translator, so a caller must not repeat it.

    RED before the deletion: `services/ingest/src/ingest/__init__.py` called
    `install_problem_handlers(app, logger)` on line 81, against an app built by `make_service_app` on
    line 51 — a second registration of the same three handlers whose only effect was to make a reader
    believe the factory did not carry them.

    The four lance-plane mains (catalog, lineage, medallion, maintenance) build their own `FastAPI`
    and must keep calling it; only an app that came out of the factory may not.
    """
    offenders = []
    for path in (REPO / "services").rglob("*.py"):
        if "/tests/" in str(path):
            continue
        source = path.read_text()
        if "make_service_app(" in source and re.search(r"^\s*install_problem_handlers\(", source, re.MULTILINE):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], offenders
