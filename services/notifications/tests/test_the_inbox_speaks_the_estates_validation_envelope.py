"""The inbox answers a 422 in the estate's one validation envelope.

`docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md` "The Python estate audit" X11: `make_service_app`
installs the Lance-Namespace problem handlers on every app it builds (`service_kit/app.py`,
`_install_ns_problem_handlers`), so a validation error answers RFC 9457 fields, the numeric spec `code` a generated
client dispatches on and the per-field `errors` list, whichever service a client called. This pins it on the inbox's
app: a factory that stopped installing them would leave it a thinner 422, well formed on its own.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from notifications import app as inbox_app


class _Body(BaseModel):
    n: int


@contextmanager
def _probe_route(app: FastAPI) -> Generator[None]:
    """Add a probe route to the REAL, module-level app and take it away again.

    The app is a process-wide singleton, so a route left behind would change what every later test in this process sees
    the app serve. Testing the real app is the point, so the route goes on and comes off.
    """
    before = list(app.router.routes)
    schema = app.openapi_schema
    try:
        yield
    finally:
        app.router.routes[:] = before
        app.openapi_schema = schema


def test_the_inbox_renders_the_estates_validation_envelope() -> None:
    """A 422 is the status a client is most likely to hit, so its body must not depend on the service."""
    with _probe_route(inbox_app):

        @inbox_app.post("/_probe_validation")
        async def _probe(_body: _Body) -> None:
            return None

        response = TestClient(inbox_app, raise_server_exceptions=False).post("/_probe_validation", json={"n": "not-an-int"})
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/problem+json"), response.headers
    body = response.json()
    assert body["title"] == "Validation Error", body
    assert body["code"] == 13, body  # ErrorCode.INVALID_INPUT — what a generated client dispatches on
    assert [entry["field"] for entry in body["errors"]] == ["body.n"], body
