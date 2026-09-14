"""One error code must not arrive at two statuses, and `INVALID_INPUT` did.

`ns_errors` maps `ErrorCode.INVALID_INPUT` to 400 in BOTH directions — `_STATUS` for outgoing domain
errors and `_STATUS_CODE_FALLBACK` for incoming framework ones — and then served that same code at 422
from `handle_validation_error`. A generated Lance client dispatches on the CODE, so one server answered
one condition two ways, and the vendored spec (re-checked against the re-vendored copy, 2026-09-14)
contains ZERO 422s: on a spec operation it is a status no generated client has a contract for.

NARROWED TO SPEC ROUTES RATHER THAN FLIPPED ESTATE-WIDE. A rask-only door — the `/credentials` vending
extension, the registries — has no lance-ns contract to honour, and its callers are this estate's own
zones and tests, which expect FastAPI's 422. Both directions are asserted here because a narrowing
that answered 400 everywhere would pass any test written only for the first.

DRIVEN THROUGH A REAL APP rather than by calling the branch: the handler reads
`request.scope["route"].path_format`, which only exists because Starlette put it there after routing.
Calling `is_spec_route` alone would pin the table and not the seam that consults it.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.lakehouse.spec_routes import SPEC_ROUTES, is_spec_route


class _Body(BaseModel):
    required_field: int


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_problem_handlers(app, log=logging.getLogger("test"))

    # `UpdateTable` — a real spec operation, spelled with a DIFFERENT path-parameter name than the
    # spec uses, because the estate and the spec need not agree on what a parameter is called.
    @app.post("/v1/table/{table_id}/update")
    async def spec_route(table_id: str, body: _Body) -> dict[str, str]:
        return {"ok": table_id}

    # rask's own vending extension: in no spec path.
    @app.post("/v1/table/{table_id}/credentials")
    async def rask_route(table_id: str, body: _Body) -> dict[str, str]:
        return {"ok": table_id}

    return TestClient(app)


def test_a_SPEC_route_answers_400_the_status_the_spec_maps_INVALID_INPUT_to(client: TestClient) -> None:
    response = client.post("/v1/table/db$t/update", json={})

    assert response.status_code == 400, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == 0 or body["code"] is not None, body
    assert body["status"] == 400, f"the envelope must agree with the wire status, or a client reads two answers: {body}"


def test_a_RASK_ONLY_route_keeps_422(client: TestClient) -> None:
    """The twin. Without it, 'answer 400 everywhere' passes the test above."""
    response = client.post("/v1/table/db$t/credentials", json={})

    assert response.status_code == 422, response.text
    assert response.json()["status"] == 422


def test_the_path_parameter_NAME_does_not_decide_it() -> None:
    """The spec writes `{id}`; this app wrote `{table_id}`. Matching on the literal pattern would make
    the status depend on a local variable name, which is the kind of coupling that works until someone
    renames a parameter."""
    assert is_spec_route("POST", "/v1/table/{table_id}/update")
    assert is_spec_route("POST", "/v1/table/{id}/update")
    assert not is_spec_route("POST", "/v1/table/{id}/credentials")


def test_a_request_that_reached_no_route_is_not_a_spec_operation() -> None:
    """`handle_validation_error` passes `""` when the scope carries no route. That must be a plain
    False, not an exception — the handler is an error path and must not raise from inside one."""
    assert not is_spec_route("POST", "")
    assert SPEC_ROUTES, "the set is empty, so every route would read as rask-only"
