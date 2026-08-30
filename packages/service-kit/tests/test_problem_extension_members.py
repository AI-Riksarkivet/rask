"""A problem body may carry EXTENSION MEMBERS, and the shared builder must not drop them.

RFC 9457 §3.2 says a problem detail object may carry additional members beyond the five it
standardises, and that consumers must ignore ones they do not recognise. `_problem` built exactly
four keys and nothing else, so a refusal whose whole point is a STRUCTURED payload — the flows
service's `422` carries the list of graph problems its builder highlights nodes from — could not use
the hierarchy at all. `flows.routes.create_run` therefore hand-built its own body and declared
`-> RunState | JSONResponse`, an escape hatch around the estate's one error plane
(FLOWS-422-BYPASSES-HIERARCHY).

Two properties, and the second is the one that makes this safe to have:

* an extension member reaches the wire, and
* it can never SHADOW a standard member — `type`, `title`, `status` and `detail` are the contract
  every client in the estate parses, and an extension that could overwrite one would turn a
  convenience into a way to lie about the status code.
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import PROBLEM_JSON, UnprocessableEntityError, register_handlers


class _Refused(UnprocessableEntityError):
    """A refusal with its own problem `type`, the way a domain names its own failure."""

    problem_type = "about:blank#test-invalid"


def _app() -> FastAPI:
    app = FastAPI()
    register_handlers(app)

    @app.get("/refuse")
    async def refuse() -> None:
        raise _Refused("2 problem(s)", extensions={"problems": ["a", "b"]})

    @app.get("/shadow")
    async def shadow() -> None:
        raise UnprocessableEntityError("nope", extensions={"status": 200, "detail": "all good", "extra": 1})

    return app


def test_an_extension_member_reaches_the_wire() -> None:
    response = TestClient(_app()).get("/refuse")

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json() == {
        "type": "about:blank#test-invalid",
        "title": "Unprocessable Entity",
        "status": 422,
        "detail": "2 problem(s)",
        "problems": ["a", "b"],
    }


def test_an_extension_member_cannot_shadow_a_standard_one() -> None:
    response = TestClient(_app()).get("/shadow")

    body = response.json()
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert body["status"] == 422, "an extension member overwrote the status the client dispatches on"
    assert body["detail"] == "nope"
    assert body["extra"] == 1


def test_the_default_problem_type_is_unchanged_for_a_plain_error() -> None:
    """No `problem_type` declared → the class-name form every existing refusal already answers."""
    app = FastAPI()
    register_handlers(app)

    @app.get("/plain")
    async def plain() -> None:
        raise UnprocessableEntityError("plain")

    body = TestClient(app).get("/plain").json()
    assert body == {
        "type": "about:blank#unprocessableentityerror",
        "title": "Unprocessable Entity",
        "status": 422,
        "detail": "plain",
    }
