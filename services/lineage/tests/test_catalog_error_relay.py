"""What lineage's caller reads when a describe lineage asked the catalog under the caller's own bearer fails.

A refusal a describe owes its caller is the caller's to read: its status, its `code`, which a Lance client dispatches on
(`lance_docs/ns_catalog/spec.yaml`, `ErrorResponse` requires it), and its `detail`, passed on rather than reclassified.
Any other answer says nothing about the caller's name. A 5xx, a 429 or a body that is no problem body of the spec's may
come from whatever stands in front of the catalog (an ingress or a proxy with an HTML page or a diagnostic of its own),
and code 0 is the catalog serving no such operation, which is lineage's request being wrong. The caller then reads code
17 and a stable sentence, and the answer reaches the operator through the log only (`exception-handlers.md`: "Never
include exception internals in the response body").

A call lineage makes as itself is the other half, driven against a live catalog by
`tests/integration/test_a_burst_of_deliveries_is_recorded_in_one_commit_per_table.py`: whatever the catalog answers it,
the caller reads the outage.
"""

import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from lance_namespace import ErrorCode
from lineage_lane import Feed, started

from lineage.services import catalog_reader


_UPSTREAM = "<html>nginx/1.25.3 upstream 10.42.0.7:2333 connect() failed</html>"
_PROBLEM_JSON = "application/problem+json"
#: An event naming one governed table, which lineage's HTTP door describes at the catalog as its caller before it records.
_EVENT: dict[str, Any] = {
    "eventType": "COMPLETE",
    "eventTime": "2026-11-22T12:00:00Z",
    "producer": "https://example.invalid/etl",
    "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
    "run": {"runId": "b1b2c3d4-0000-4000-8000-000000000001"},
    "job": {"namespace": "etl", "name": "daily"},
    "outputs": [{"namespace": "acme", "name": "acme$raw$pages"}],
}


def _problem(code: int, status: int, detail: str) -> str:
    return json.dumps({"type": "https://lance.org/problems/x", "title": "X", "status": status, "detail": detail, "code": code, "error": detail})


class _Catalog:
    """A catalog on a loopback port answering every POST with ``status`` and ``body``."""

    def __init__(self) -> None:
        self.status = 200
        self.body = b"{}"
        self.content_type = _PROBLEM_JSON
        answer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("content-length", 0)))
                self.send_response(answer.status)
                self.send_header("content-type", answer.content_type)
                self.send_header("content-length", str(len(answer.body)))
                self.end_headers()
                self.wfile.write(answer.body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the stdlib's own parameter name
                del format, args

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Catalog]:
    stub = _Catalog()
    monkeypatch.setenv("LINEAGE_CATALOG_URL", stub.url)
    yield stub
    stub.close()


def test_lineages_door_answers_the_catalogs_refusal_of_the_callers_describe_as_the_catalog_sent_it(catalog: _Catalog, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through lineage's own app: what the catalog refuses the caller's name with reaches the caller in the spec's body
    as the catalog sent it, and the event is not recorded."""
    catalog.status, catalog.body = 400, _problem(ErrorCode.INVALID_INPUT, 400, "a table id has three segments").encode()

    with started(monkeypatch, Feed()) as lineage:
        refused = lineage.client.post("/api/v1/lineage", json=_EVENT)

    assert (refused.status_code, refused.headers["content-type"]) == (400, _PROBLEM_JSON), refused.text
    body = refused.json()
    assert (body["type"], body["status"], body["code"], body["detail"]) == (
        "https://lance.org/problems/cataloganswerederror",
        400,
        ErrorCode.INVALID_INPUT,
        "a table id has three segments",
    )
    assert lineage.feed.recorded == []


@pytest.mark.parametrize(
    ("status", "answer", "content_type"),
    [
        pytest.param(502, _UPSTREAM, "text/html", id="a-proxys-page"),
        pytest.param(502, _UPSTREAM * 20, "text/html", id="a-page-longer-than-the-log-keeps"),
        pytest.param(404, _UPSTREAM, "text/html", id="a-404-that-is-no-problem-body"),
        pytest.param(400, _problem(99, 400, "no such code"), _PROBLEM_JSON, id="a-code-the-spec-does-not-define"),
        pytest.param(406, _problem(ErrorCode.UNSUPPORTED, 406, "Not Acceptable"), _PROBLEM_JSON, id="an-operation-the-catalog-does-not-serve"),
        pytest.param(422, _problem(ErrorCode.INVALID_INPUT, 422, "Validation Error"), _PROBLEM_JSON, id="a-status-its-code-does-not-map-to"),
    ],
)
def test_lineages_door_answers_anything_but_a_catalog_refusal_as_the_outage_and_keeps_the_answer_in_the_log(
    status: int, answer: str, content_type: str, catalog: _Catalog, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Through lineage's own app: an answer that is no refusal a describe owes its caller reaches the caller as code 17
    naming nothing of what answered, whose first 512 characters the log keeps as one warning."""
    caplog.set_level(logging.WARNING, logger=catalog_reader.__name__)
    catalog.status, catalog.body, catalog.content_type = status, answer.encode(), content_type

    with started(monkeypatch, Feed()) as lineage:
        unanswered = lineage.client.post("/api/v1/lineage", json=_EVENT)

    assert (unanswered.status_code, unanswered.headers["content-type"]) == (503, _PROBLEM_JSON), unanswered.text
    body = unanswered.json()
    assert {member: value for member, value in body.items() if member != "instance"} == {
        "type": "https://lance.org/problems/cataloganswerederror",
        "title": "CatalogAnsweredError",
        "status": 503,
        "detail": "Internal Server Error",
        "code": ErrorCode.SERVICE_UNAVAILABLE,
        "error": "Internal Server Error",
    }
    kept = [(record.levelname, getattr(record, "body", None)) for record in caplog.records if record.name == catalog_reader.__name__]
    assert kept == [("WARNING", answer[:512])]
    assert lineage.feed.recorded == []
