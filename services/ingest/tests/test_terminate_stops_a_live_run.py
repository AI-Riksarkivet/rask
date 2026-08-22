"""Nothing could stop a live ingest run — `DWF-MGT-003`, the one critical from the 2026-08-16 review.

`open_ingest_design.md` §6: "`api.py` exposes start and status and NO LIFECYCLE CONTROL AT ALL — no
route calls `terminate_workflow`, `pause_workflow` or `resume_workflow`. The only `terminate_workflow`
in the service is `terminate_chunks`, which the PARENT calls against its own children; there is no path
by which an operator stops the parent."

The cost is specific. A run that is WRONG rather than broken — pointed at the wrong prefix, or
enumerating a bucket somebody meant to narrow — cannot be stopped. It holds its JetStream subject and
its per-run durable, and it keeps committing. Neither thing that looks like a brake is one:

  * `max_units` refuses at ENUMERATION, before the fan-out, so it cannot help a run already draining.
  * `max_run_hours` is a deadline, not a brake, and its in-code default is 0 = UNBOUNDED. Only the
    chart opts in, at 24h — so on any deployment that does not set it there is no bound whatsoever.

TERMINATE IS BOUNDED, NOT INSTANT, and the route must not promise otherwise. The SDK limit
`terminate_chunks` already documents applies here: terminate stops further SCHEDULING and does not
stop an IN-FLIGHT activity, so a `drain_chunk` mid-fetch runs to completion. A route that answered
"stopped" would be lying to the operator at the exact moment they are deciding whether to also go
revoke a credential.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ingest import api


class _Terminator:
    """The seam, structurally faked — the estate's pattern for anything needing a sidecar."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._raises = raises

    def terminate(self, run_id: str) -> bool:
        self.calls.append(run_id)
        if self._raises is not None:
            raise self._raises
        return True


class _Store:
    def __init__(self, known: set[str]) -> None:
        self.known = known

    async def get(self, run_id: str) -> Any:
        if run_id not in self.known:
            return None
        from ingest.runs import RunRecord

        return RunRecord(run_id=run_id, kind="dummy", project="acme", dataset="bronze$events")

    async def put(self, record: Any) -> None:
        return None


@pytest.fixture
def client_and_terminator(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _Terminator]:
    term = _Terminator()
    app = FastAPI()
    app.include_router(api.router, prefix="/v1")
    app.state.run_store = _Store({"run-1"})
    app.state.workflow_terminator = term
    app.state.workflow_reader = None

    async def _allow(*a: object, **k: object) -> str | None:
        return "user-1"

    monkeypatch.setattr(api, "authorize_ingest", _allow)
    return TestClient(app, raise_server_exceptions=False), term


class TestTheDoorExists:
    def test_a_terminate_route_is_registered(self) -> None:
        paths = {getattr(r, "path", "") for r in api.router.routes}
        assert "/ingests/{run_id}/terminate" in paths, "there is still no path by which an operator stops a running ingest"

    def test_it_is_a_POST(self) -> None:
        methods = {m for r in api.router.routes if getattr(r, "path", "") == "/ingests/{run_id}/terminate" for m in getattr(r, "methods", set())}
        assert methods == {"POST"}, f"terminate changes state; it is not a GET: {methods}"


class TestItActuallyTerminates:
    def test_it_reaches_the_engine_with_the_run_id(self, client_and_terminator: tuple[TestClient, _Terminator]) -> None:
        client, term = client_and_terminator

        resp = client.post("/v1/ingests/run-1/terminate")

        assert resp.status_code == 202, resp.text
        assert term.calls == ["run-1"]

    def test_an_unknown_run_is_404_not_a_silent_success(self, client_and_terminator: tuple[TestClient, _Terminator]) -> None:
        client, term = client_and_terminator

        assert client.post("/v1/ingests/nope/terminate").status_code == 404
        assert term.calls == [], "a 404 must not have reached the engine"


class TestItDoesNotOverpromise:
    def test_the_response_says_terminate_is_not_immediate(self, client_and_terminator: tuple[TestClient, _Terminator]) -> None:
        """The operator is deciding whether to ALSO go revoke a credential. "stopped" would be a lie
        while a drain_chunk is still mid-fetch."""
        client, _ = client_and_terminator

        body = client.post("/v1/ingests/run-1/terminate").json()

        assert body, "an empty body tells an operator nothing"
        text = str(body).lower()
        assert "in-flight" in text or "in flight" in text or "not immediate" in text or "may still" in text, (
            f"the response must state that in-flight work continues: {body}"
        )

    def test_the_status_code_is_202_not_200(self, client_and_terminator: tuple[TestClient, _Terminator]) -> None:
        """202 Accepted is the honest code for a request whose effect is not complete on return."""
        client, _ = client_and_terminator
        assert client.post("/v1/ingests/run-1/terminate").status_code == 202


class TestTheDoorIsGoverned:
    def test_it_authorizes_like_every_other_write(self) -> None:
        """§6 says the route sits "behind the `AuthSettingsDep` the other doors already carry". A
        terminate anyone can call is a denial-of-service on every running harvest."""
        source = inspect.getsource(api.terminate_ingest)
        assert "authorize_ingest" in source, "terminate is an unguarded door"

    def test_it_authorizes_against_the_RUNS_project_not_a_configured_one(self) -> None:
        """The same rule `create_ingest` states: authorization scope must equal write scope, or an
        admin of project A can stop project B's run."""
        source = inspect.getsource(api.terminate_ingest)
        assert "record.project" in source, "the admin check must target the project the run belongs to"


class TestBlockingWorkStaysOffTheLoop:
    def test_the_engine_call_is_not_awaited_inline(self) -> None:
        """`DaprWorkflowClient` is synchronous. Calling it directly in an `async def` blocks the event
        loop — the same rule `get_ingest` already follows with `asyncio.to_thread(reader.state, ...)`."""
        source = inspect.getsource(api.terminate_ingest)
        assert "to_thread" in source, "a sync SDK call inside async def blocks every other request"
