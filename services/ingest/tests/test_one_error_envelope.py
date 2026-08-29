"""ING-03 — the router answers with ONE error envelope, not two.

`create_app` installs two problem+json translators (`service_kit.exceptions.register_handlers` for
the fleet's `DomainError`s, `install_problem_handlers` for the Lance-Namespace ones), and the
control API's own refusals — an unknown source kind, a refused sizing, an idempotency conflict, a
busy workflow engine, an unknown run — are raised as plain `fastapi.HTTPException`. Those slipped
past both and were served by starlette's default handler as `application/json` `{"detail": …}`.

So a client of ONE router had to parse two unrelated shapes, and which one it got depended on which
line of the handler raised. These pin the whole set on the RFC 9457 envelope the rest of the estate
answers with — media type included, and with the `Retry-After` the 503 exists to carry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from ingest import create_app
from ingest.runs import ScheduleUnavailable
from ingest.sources import register


if TYPE_CHECKING:
    from collections.abc import Iterator

    from service_kit.lakehouse.sources import SourceObject

PROBLEM_JSON = "application/problem+json"
BODY = {"kind": "envelope-src", "project": "p1", "dataset": "pages", "options": {}}


class _NoUnits:
    def iter_objects(self) -> Iterator[SourceObject]:
        return iter(())


@pytest.fixture(autouse=True)
def _register_test_source() -> None:
    from ingest.sources import LineageInput, registered_kinds

    if "envelope-src" not in registered_kinds():
        register(
            "envelope-src",
            build=lambda spec: _NoUnits(),
            lineage_input=lambda spec: LineageInput(namespace="test", name=spec.dataset),
        )


class _Starter:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail

    async def start(self, run_id: str, payload: dict[str, object]) -> None:
        if self.fail is not None:
            raise self.fail


def _app(monkeypatch: pytest.MonkeyPatch, starter: Any = None) -> TestClient:
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    app = create_app()
    app.state.workflow_starter = starter or _Starter()
    return TestClient(app)


def _assert_problem(res: Any, status: int) -> dict[str, Any]:
    assert res.status_code == status, res.text
    assert res.headers["content-type"].split(";")[0] == PROBLEM_JSON, (
        f"{status} came back as {res.headers['content-type']!r}, not the estate's problem+json envelope: {res.text}"
    )
    body = res.json()
    assert {"type", "title", "status", "detail"} <= set(body), f"not an RFC 9457 body: {body}"
    assert body["status"] == status
    return body


def test_an_unknown_source_kind_is_refused_in_problem_json(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _app(monkeypatch)
    res = c.post("/api/ingests", json={**BODY, "kind": "no-such-kind"}, headers={"Idempotency-Key": "k"})
    assert "no-such-kind" in _assert_problem(res, 400)["detail"]


def test_an_unknown_run_is_refused_in_problem_json(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _app(monkeypatch)
    _assert_problem(c.get("/api/ingests/nope"), 404)


def test_a_reused_key_naming_another_spec_conflicts_in_problem_json(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _app(monkeypatch)
    assert c.post("/api/ingests", json=BODY, headers={"Idempotency-Key": "reused"}).status_code == 202
    res = c.post("/api/ingests", json={**BODY, "dataset": "other"}, headers={"Idempotency-Key": "reused"})
    _assert_problem(res, 409)


def test_a_busy_engine_keeps_its_retry_after_in_problem_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 503 is only actionable with its `Retry-After`, so the envelope must not eat the headers."""
    c = _app(monkeypatch, _Starter(ScheduleUnavailable("sidecar refused")))
    res = c.post("/api/ingests", json=BODY, headers={"Idempotency-Key": "busy"})
    _assert_problem(res, 503)
    assert res.headers.get("Retry-After") == "5"


def test_a_terminate_of_an_unknown_run_is_refused_in_problem_json(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _app(monkeypatch)
    _assert_problem(c.post("/api/ingests/nope/terminate"), 404)
