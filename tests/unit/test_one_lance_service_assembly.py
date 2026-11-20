"""The lance-plane entrypoints are assembled once (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-12).

DUP-12: "The lance-service entrypoint is hand-assembled eight times; there is no
`make_lance_service_app`." Each opened `app = FastAPI(...)` at module level and then repeated the same five
steps in prose comments copied between them.

This pins what `lakehouse.service_app.factory` gives catalog, lineage and maintenance: the copies had drifted, one service
serving no correlation layer at all, and nothing compared them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import UnauthenticatedError
from starlette.types import Message, Scope

from lakehouse.process.correlation import current_trace_id


LANCE_APPS = (
    ("catalog", "catalog.main"),
    ("lineage", "lineage.main"),
    ("maintenance", "maintenance.worker"),
)


@pytest.fixture(autouse=True)
def _catalog_needs_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """`catalog.main` builds its settings at import, and the access key id has no default."""
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "x")


@contextmanager
def _probe_route(app: FastAPI) -> Generator[None]:
    """Add a probe route to a REAL, module-level app and take it away again.

    These apps are process-wide singletons — `catalog.main.app` is imported by every test that touches
    the catalog — so a route added here and left behind changes what
    `tests/unit/test_openapi_contract.py` sees the live app serve, and that test then fails for a
    reason that has nothing to do with it. Testing the real app is the point (a throwaway app built
    the same way proves only that the builder works), so the route goes on and comes off.
    """
    before = list(app.router.routes)
    schema = app.openapi_schema
    try:
        yield
    finally:
        app.router.routes[:] = before
        app.openapi_schema = schema


def _app(module_path: str) -> FastAPI:
    module = __import__(module_path, fromlist=["app"])
    app = module.app
    assert isinstance(app, FastAPI)
    return app


#: A caller's own trace, as W3C Trace Context's example writes one.
_CALLERS_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.mark.parametrize(
    ("name", "module_path", "sent"),
    [
        *[pytest.param(name, module, None, id=f"{name}-no-trace-sent") for name, module in LANCE_APPS],
        # The catalog's continuing of a caller's trace is pinned through its doors
        # (`test_every_event_and_commit_a_write_causes_carries_the_trace_of_the_request_that_caused_it`).
        *[
            pytest.param(name, module, f"00-{_CALLERS_TRACE}-00f067aa0ba902b7-01", id=f"{name}-the-callers-trace")
            for name, module in LANCE_APPS
            if name != "catalog"
        ],
    ],
)
def test_every_lance_app_runs_each_request_in_a_trace(name: str, module_path: str, sent: str | None) -> None:
    """Every lance app runs each request in a trace, the caller's where it sent one (ADR 0209): its id is the one
    correlation id, so a request with no ``traceparent`` of its own still has one whether or not an exporter is set.

    Driven through a REQUEST rather than by looking for the instrumentation on the app: what a handler reads as the
    current trace is the contract.
    """
    app = _app(module_path)
    with _probe_route(app):

        @app.get("/_probe_trace")
        async def _trace() -> dict[str, str | None]:
            return {"trace": current_trace_id()}

        trace_id = TestClient(app, raise_server_exceptions=False).get("/_probe_trace", headers={"traceparent": sent} if sent else {}).json()["trace"]
    assert (trace_id == _CALLERS_TRACE) if sent else (trace_id is not None and len(trace_id) == 32), trace_id


@pytest.mark.parametrize(("name", "module_path"), [row for row in LANCE_APPS if row[0] != "lineage"], ids=["catalog", "maintenance"])
def test_every_lance_app_refuses_a_body_over_its_cap_before_reading_it(name: str, module_path: str) -> None:
    """The app factory takes the body cap as a required keyword and installs it (CTL-006), so a body declared over it is
    refused 413 from its Content-Length, before a byte of it is read. Lineage's is driven over a socket, where its
    callers reach it: `services/lineage/tests/test_a_post_to_the_intake_is_refused_before_its_body_is_read.py`."""
    answered: list[Message] = []
    reads = 0

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        answered.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/table/t/insert",
        "raw_path": b"/v1/table/t/insert",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"content-length", str(2**40).encode())],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }

    asyncio.run(_app(module_path)(scope, receive, send))

    start, body = answered[0], json.loads(answered[1]["body"])
    assert (start["status"], body["code"], reads) == (413, 13, 0), answered


@pytest.mark.parametrize(("name", "module_path"), LANCE_APPS, ids=[row[0] for row in LANCE_APPS])
def test_every_lance_app_translates_a_lance_error(name: str, module_path: str) -> None:
    """The handler pair, in the order that makes it work — one place rather than copied blocks."""
    app = _app(module_path)
    with _probe_route(app):

        @app.get("/_probe_unauthenticated")
        async def _raise() -> None:
            raise UnauthenticatedError("expired token")

        response = TestClient(app, raise_server_exceptions=False).get("/_probe_unauthenticated")
    assert response.status_code == 401, response.text
    assert response.headers["content-type"].startswith("application/problem+json"), response.headers


def test_the_catalog_maintenance_gate_answers_inside_the_requests_trace() -> None:
    """The read-only gate answers inside the request's trace, so its 503 names the trace (the problem body's
    ``instance``) an operator correlates the refusal with (ADR 0209)."""
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    app = _app("catalog.main")
    client = TestClient(app, raise_server_exceptions=False)
    # The gate reads the setting per request, so flipping it needs no rebuild — and it is put BACK,
    # because `app` is the process-wide catalog app every other test in this run also imports.
    original = getattr(app.state, "settings", None)
    app.state.settings = get_settings().model_copy(update={"maintenance_read_only": True})
    try:
        response = client.post("/v1/namespace/x/create", json={}, headers={"traceparent": f"00-{_CALLERS_TRACE}-00f067aa0ba902b7-01"})
    finally:
        app.state.settings = original
    assert (response.status_code, response.json().get("instance")) == (503, _CALLERS_TRACE), response.text
