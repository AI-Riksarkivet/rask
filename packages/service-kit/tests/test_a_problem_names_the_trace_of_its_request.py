"""[[LH-485]] A problem body names its occurrence: ``instance`` is the W3C trace id of the request it answers.

A 5xx detail is redacted to "Internal Server Error", so without an id the caller has nothing to give an operator. The
spec's ``ErrorResponse.instance`` is that id (`lance_docs/ns_catalog/spec.yaml`), and the request's own trace is where the
redacted error's log line sits. Driven through `install_problem_handlers` on an app the FastAPI instrumentor wraps, as
`service_kit.otel.setup_otel` wraps every service, with the caller's ``traceparent`` naming the trace.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import TableNotFoundError
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider

from service_kit.lakehouse.ns_errors import install_problem_handlers


TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = {"traceparent": f"00-{TRACE_ID}-00f067aa0ba902b7-01"}


def _app(*, traced: bool) -> FastAPI:
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger("test"))

    @app.get("/missing")
    def missing() -> None:
        raise TableNotFoundError("Table not found: g$t")

    @app.get("/crash")
    def crash() -> None:
        raise RuntimeError("a fault whose text is never answered")

    if traced:
        FastAPIInstrumentor.instrument_app(app, tracer_provider=TracerProvider())
    return app


@pytest.mark.parametrize(("path", "status"), [pytest.param("/missing", 404, id="a-domain-error"), pytest.param("/crash", 500, id="a-redacted-fault")])
def test_a_problem_names_the_trace_of_its_request(path: str, status: int) -> None:
    response = TestClient(_app(traced=True), raise_server_exceptions=False).get(path, headers=TRACEPARENT)

    assert (response.status_code, response.json().get("instance")) == (status, TRACE_ID), response.text


def test_a_problem_names_no_occurrence_when_nothing_traces_the_request() -> None:
    response = TestClient(_app(traced=False)).get("/missing", headers=TRACEPARENT)

    assert "instance" not in response.json(), response.text
