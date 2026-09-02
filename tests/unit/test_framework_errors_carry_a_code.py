"""A 404 or 405 from the framework must still carry the spec's `code`.

Every error body on a `/v1` route is parsed by a generated Lance client, whose `ErrorResponse` model
REQUIRES `code`. rask installs handlers for `LanceNamespaceError`, `RequestValidationError` and bare
`Exception`, so those three are coded — but FastAPI answers its own routing failures through
`StarletteHTTPException`, which no handler claimed. Measured at HEAD before this landed:

    DELETE /v1/table/db$t/describe  -> 405  {"detail":"Method Not Allowed"}
    POST   /v1/table/db$t/no-such   -> 404  {"detail":"Not Found"}

Neither carries `code`, so the reference client cannot parse either and raises `InternalError 18`
("Failed to parse error response … missing field `code`") — the client is told the server broke when
what actually happened is that it does not serve that route. That was A3's real cost too: the GET
form of `count_rows` produced exactly this 405, and the client reported an internal error.

WHY `Unsupported` (code 0) FOR BOTH. The spec's codes are domain answers, and neither "no such route"
nor "wrong method here" is a domain condition — but both are precisely "this backend does not do that
operation", which is what code 0 means. A `TableNotFound` 404 is unaffected: it is raised as a
`LanceNamespaceError` and answered with code 4 by the handler above this one.
"""

from __future__ import annotations

import json

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from lance_namespace import ErrorCode

from service_kit.lakehouse.ns_errors import PROBLEM_JSON, install_problem_handlers


@pytest.fixture
def app() -> FastAPI:
    import logging

    application = FastAPI()
    router = APIRouter()

    @router.post("/v1/thing/{id}/describe")
    def describe(id: str) -> dict[str, str]:
        return {"id": id}

    @router.get("/v1/boom")
    def boom() -> None:
        raise HTTPException(status_code=409, detail="already there")

    application.include_router(router)
    install_problem_handlers(application, logging.getLogger("test"))
    return application


def test_a_routing_404_carries_a_code(app: FastAPI) -> None:
    with TestClient(app) as client:
        resp = client.post("/v1/thing/x/no-such-op")
    assert resp.status_code == 404
    body = resp.json()
    assert body.get("code") == ErrorCode.UNSUPPORTED, f"no parseable code on a routing 404: {body}"
    assert resp.headers["content-type"].startswith(PROBLEM_JSON)


def test_a_405_carries_a_code(app: FastAPI) -> None:
    """The exact body A3's GET form produced before the dual-mount."""
    with TestClient(app) as client:
        resp = client.delete("/v1/thing/x/describe")
    assert resp.status_code == 405
    assert resp.json().get("code") == ErrorCode.UNSUPPORTED, f"no code on a 405: {resp.json()}"


def test_an_explicit_http_exception_is_coded_by_its_status(app: FastAPI) -> None:
    """An HTTPException raised by app code keeps its status and gains the nearest spec code, so a
    client can still dispatch. Its detail survives — that is the part an operator reads."""
    with TestClient(app) as client:
        resp = client.get("/v1/boom")
    assert resp.status_code == 409
    body = resp.json()
    assert "code" in body, body
    assert body["detail"] == "already there"


def test_the_body_is_valid_rfc9457_and_parses_as_json(app: FastAPI) -> None:
    with TestClient(app) as client:
        resp = client.post("/v1/thing/x/no-such-op")
    body = json.loads(resp.text)
    assert {"type", "title", "status", "code"} <= set(body), body
    assert body["status"] == 404


def test_a_domain_404_is_untouched(app: FastAPI) -> None:
    """A `TableNotFound` must still answer code 4 — this handler sits BELOW the domain one and must
    not swallow it, which is the way a catch-all like this usually goes wrong."""
    from lance_namespace import TableNotFoundError

    router = APIRouter()

    @router.post("/v1/thing/{id}/domain")
    def domain(id: str) -> None:
        raise TableNotFoundError("nope")

    app.include_router(router)
    with TestClient(app) as client:
        resp = client.post("/v1/thing/x/domain")
    assert resp.status_code == 404
    assert resp.json()["code"] == ErrorCode.TABLE_NOT_FOUND, resp.json()
