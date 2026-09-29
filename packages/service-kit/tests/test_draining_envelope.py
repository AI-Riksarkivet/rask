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

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from service_kit.draining import refuse_when_draining


PROBLEM_JSON = "application/problem+json"


#: Every app that mounts `refuse_when_draining`. All are on the lance plane, which is why the
#: `DomainError`-subclasses-`HTTPException` trap bit here specifically.
#: The five lance-plane deployables, by importable module. They all assemble through
#: `service_kit.lance_app.build_lance_service_app` since the DUP-12 collapse; before it, each hand-wrote
#: the handler pair and this list was five file PATHS grepped for `register_handlers(app)`. A grep is
#: the wrong instrument for the question — it passes on a mention in a comment and fails on a correct
#: app that got its handlers from a factory — so it is now driven through the real apps.
DRAINING_APPS = [
    ("medallion-producer", "medallion.producer"),
    ("medallion-stage-runner", "medallion.stage_runner"),
    ("lineage", "lineage.main"),
    ("maintenance", "maintenance.service"),
    ("catalog", "catalog.main"),
]


@pytest.mark.parametrize(("name", "module_path"), DRAINING_APPS, ids=[row[0] for row in DRAINING_APPS])
def test_every_lance_app_maps_domain_errors_too(name: str, module_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The half a unit test on the dependency cannot cover.

    `DomainError` subclasses `HTTPException`, so an app with only the ns translator still ANSWERS —
    503, `Retry-After` intact — with a `{"detail": ...}` body. Raising the right class is therefore not
    sufficient; the app has to install `register_handlers` for the envelope to exist at all. These five
    apps installed only the translator until this finding, and every door using this dependency is on
    one of them.
    """
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "x")  # `catalog.main` builds its settings at import
    module = __import__(module_path, fromlist=["app"])
    app = module.app
    # The route comes OFF again: these are process-wide singletons, and a probe left on one changes
    # what `tests/unit/test_openapi_contract.py` sees the live app serve.
    routes_before = list(app.router.routes)
    schema_before = app.openapi_schema

    @app.get("/_probe_draining", dependencies=[Depends(refuse_when_draining)])
    async def _guarded() -> dict[str, str]:
        return {"ok": "yes"}

    app.state.shutting_down = True
    try:
        response = TestClient(app, raise_server_exceptions=False).get("/_probe_draining")
    finally:
        app.state.shutting_down = False
        app.router.routes[:] = routes_before
        app.openapi_schema = schema_before
    assert response.status_code == 503, response.text
    assert response.headers["content-type"].startswith(PROBLEM_JSON), dict(response.headers)
    assert response.headers.get("Retry-After"), dict(response.headers)
    body = response.json()
    assert {"type", "title", "status", "detail"} <= set(body), body
