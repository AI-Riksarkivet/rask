"""A draining 503 must have the shape its Content-Type claims.

open_fastapi-audit — "draining.py stamps content-type: application/problem+json on a body that is only
{"detail": …} — the three medallion write doors lie about their 503's shape".

`refuse_when_draining` raised `HTTPException(503, detail=..., headers={..., "Content-Type":
"application/problem+json"})`. The status and `Retry-After` — the two things a retrying caller acts on
— were correct; the BODY was FastAPI's `{"detail": ...}` wearing a media type that asserts
`{type,title,status,detail}`. A header that renames a body without changing it is worse than no
header: it is the one thing a client parses before deciding how to read the payload.

WHY IT WAS DONE THAT WAY, and why that reason has expired. The docstring said raising "is not an
option here — the caller needs the `Retry-After` header, and an exception handler would have to
reconstruct it". That was true when it was written. `DomainError` now carries `headers` and
`register_handlers` passes them through, so a raised `ServiceUnavailableError` keeps its header AND
gets the real envelope.

THE TRAP THIS UNCOVERED, which is why the fix is not one line: `DomainError` subclasses
`HTTPException`. An app that installs only `install_problem_handlers` — which is every lance app,
including the three medallion doors this finding names — renders it through starlette's built-in
handler instead: status 503 and `Retry-After` survive, and the body is `{"detail": ...}` again. So
raising the right class is not enough; the app has to map it.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from service_kit.draining import refuse_when_draining
from service_kit.exceptions import register_handlers
from service_kit.lakehouse.ns_errors import install_problem_handlers


PROBLEM_JSON = "application/problem+json"


def _app(*, lance_plane: bool) -> FastAPI:
    """An app composed like the real ones. The lance plane installs the ns translator too, and — since
    this finding — `register_handlers` as well, which is what makes a `DomainError` render as a problem
    document there rather than through starlette's `HTTPException` fallback."""
    app = FastAPI()
    register_handlers(app)
    if lance_plane:
        install_problem_handlers(app, logging.getLogger(__name__))

    @app.post("/run", dependencies=[Depends(refuse_when_draining)])
    async def run() -> dict[str, str]:
        return {"status": "started"}

    app.state.shutting_down = True
    return app


@pytest.mark.parametrize("lance_plane", [False, True], ids=["fleet", "lance-plane"])
def test_the_draining_refusal_is_really_problem_json(lance_plane: bool) -> None:
    """Both planes, because the three doors this finding names are on the lance side."""
    response = TestClient(_app(lance_plane=lance_plane), raise_server_exceptions=False).post("/run")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith(PROBLEM_JSON), (
        f"the 503 declares {response.headers['content-type']} — a Content-Type that renames a body without changing it is worse than none"
    )
    body = response.json()
    assert {"type", "title", "status", "detail"} <= set(body), f"the body is {sorted(body)} while its media type asserts a problem document"


@pytest.mark.parametrize("lance_plane", [False, True], ids=["fleet", "lance-plane"])
def test_retry_after_survives(lance_plane: bool) -> None:
    """The half that already worked must keep working — it is what a retrying caller acts on, and the
    reason the original chose `HTTPException` in the first place."""
    response = TestClient(_app(lance_plane=lance_plane), raise_server_exceptions=False).post("/run")
    assert response.headers["Retry-After"], "the drain window's Retry-After was dropped"


def test_a_healthy_app_is_untouched() -> None:
    app = _app(lance_plane=False)
    app.state.shutting_down = False
    assert TestClient(app).post("/run").status_code == 200


#: Every app that mounts `refuse_when_draining`. All are on the lance plane, which is why the
#: `DomainError`-subclasses-`HTTPException` trap bit here specifically.
DRAINING_APPS = [
    "services/medallion/src/medallion/producer.py",
    "services/medallion/src/medallion/mover.py",
    "services/lineage/src/lineage/main.py",
    "services/maintenance/src/maintenance/service.py",
    "services/catalog/src/catalog/main.py",
]


@pytest.mark.parametrize("path", DRAINING_APPS, ids=[p.split("/")[1] for p in DRAINING_APPS])
def test_every_lance_app_maps_domain_errors_too(path: str) -> None:
    """The half a unit test on the dependency cannot cover.

    `DomainError` subclasses `HTTPException`, so an app with only the ns translator still ANSWERS —
    503, `Retry-After` intact — with a `{"detail": ...}` body. Raising the right class is therefore not
    sufficient; the app has to install `register_handlers` for the envelope to exist at all. These five
    apps install only the translator until this finding, and every door using this dependency is on one
    of them.
    """
    import pathlib as _p

    source = (_p.Path(__file__).resolve().parents[3] / path).read_text()
    assert "register_handlers(app)" in source, (
        f"{path} installs only the lance translator, so a DomainError renders through starlette's HTTPException fallback — status right, envelope absent"
    )
