"""Incremental ingest was built and could only be started by hand.

`open_ingest_design.md` §1c chose the anti-join so incremental runs need no second store, and the
mechanism shipped: `enumerate_chunks` opens bronze, projects the id column, and lands only what is
new. What never shipped was TRIGGER-2 — the thing that makes it happen on its own. Until now the only
way a run started was a manual `POST /v1/ingests`, which is the exact state the section described at
HEAD 50e5b684.

The ordering mattered and is why this comes last rather than first. Putting an unbounded
O(existing rows) read on a clock would have turned a per-request cost into a recurring one; the
`RASK_INGEST_INCREMENTAL_MAX_ROWS` ceiling had to exist first, and now does.

IT IS A POLL, and the design says to say so out loud — event-driven from bronze inward, a scheduled
poll at the outer boundary. Bucket notification was rejected as the general mechanism because it
covers one of three registered kinds and IIIF has no notification channel and never will.

THE AUTHORIZATION CONSTRAINT IS REAL AND DESIGNED. A tick carries no user, so the run authorizes on
the service-token branch, which is pinned to the configured service project. A multi-tenant watch set
cannot work through that door as it stands, and nothing carries a watch creator's authority forward
to fire time — so this trigger serves ONE project by construction, and the tests say so rather than
letting someone discover it from a 403.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ingest.cron import build_incremental_cron_router
from ingest.runs import RunRecord


class TestTheRouteMatchesTheBindingName:
    """Dapr POSTs to `/<component name>` at the pod ROOT — the component name, the env var and the
    served path are one string, and a mismatch is a binding that fires into a 404 forever."""

    def _client(self, binding: str = "ingest-incremental-cron") -> TestClient:
        app = FastAPI()
        app.include_router(build_incremental_cron_router(binding))
        return TestClient(app, raise_server_exceptions=False)

    def test_the_post_route_is_the_binding_name(self) -> None:
        paths = {getattr(r, "path", "") for r in build_incremental_cron_router("my-binding").routes}
        assert "/my-binding" in paths

    def test_it_answers_the_OPTIONS_discovery_preflight(self) -> None:
        """Dapr pre-flights a binding with OPTIONS. Without an ack the sidecar logs the binding as
        unregistered and never delivers."""
        assert self._client().options("/ingest-incremental-cron").status_code < 400

    def test_it_is_mounted_at_the_ROOT_not_under_the_api_prefix(self) -> None:
        """The sidecar delivers to the pod root; a route under `/api` is a route Dapr never calls."""
        paths = {getattr(r, "path", "") for r in build_incremental_cron_router("b").routes}
        assert not any(p.startswith("/api") for p in paths)


class TestItIsMountedOnlyWhenConfigured:
    def test_no_binding_name_mounts_nothing(self) -> None:
        """A cron route with no cron is an unauthenticated door into starting ingest runs."""
        from ingest.cron import mount_incremental_cron

        app = FastAPI()
        assert mount_incremental_cron(app, None) is False
        assert mount_incremental_cron(app, "") is False

    def test_a_named_binding_mounts_it(self) -> None:
        from ingest.cron import mount_incremental_cron

        app = FastAPI()
        assert mount_incremental_cron(app, "ingest-incremental-cron") is True


class TestItSaysItIsAPoll:
    def test_the_docstring_names_the_poll(self) -> None:
        """§1c asks for this explicitly, because a reader of the workflow module would otherwise
        conclude the plane never polls anywhere."""
        import inspect

        from ingest import cron

        text = (inspect.getdoc(cron) or "") + (inspect.getdoc(cron.build_incremental_cron_router) or "")
        assert "poll" in text.lower()

    def test_the_single_project_constraint_is_stated(self) -> None:
        """The tick authorizes on the service-token branch, pinned to one project. Discovering that
        from a 403 at 3am is the outcome this sentence exists to prevent."""
        import inspect

        from ingest import cron

        assert "project" in (inspect.getdoc(cron) or "").lower()


class TestTheTickActuallyDispatches:
    """A cron route that only logged would be a door that does nothing — the shape this trigger
    exists to replace, wearing a schedule."""

    def _app(self, monkeypatch: pytest.MonkeyPatch, *, started: list[tuple[str, dict]]) -> TestClient:
        from ingest.adapters import register_builtin_sources
        from ingest.cron import mount_incremental_cron

        register_builtin_sources()
        monkeypatch.setenv("RASK_INGEST_CRON_KIND", "local-dir")
        monkeypatch.setenv("RASK_INGEST_CRON_DATASET", "pages")
        monkeypatch.setenv("RASK_INGEST_SERVICE_PROJECT", "acme")
        monkeypatch.delenv("APP_API_TOKEN", raising=False)

        class _Starter:
            async def start(self, run_id: str, payload: dict) -> None:
                started.append((run_id, payload))

        class _Store:
            def __init__(self) -> None:
                self.records: dict[str, RunRecord] = {}

            async def get(self, run_id: str) -> RunRecord | None:
                return self.records.get(run_id)

            async def put(self, record: RunRecord) -> None:
                self.records[record.run_id] = record

        app = FastAPI()
        mount_incremental_cron(app, "ingest-incremental-cron")
        app.state.run_store = _Store()
        app.state.workflow_starter = _Starter()
        return TestClient(app, raise_server_exceptions=False)

    def test_a_tick_starts_a_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        started: list[tuple[str, dict]] = []
        client = self._app(monkeypatch, started=started)

        body = client.post("/ingest-incremental-cron").json()

        assert body["status"] == "SUCCESS"
        assert started, f"the tick dispatched nothing: {body}"
        assert started[0][1]["kind"] == "local-dir"
        assert started[0][1]["dataset"] == "pages"

    def test_two_ticks_are_two_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The opposite of the HTTP door's contract, and correct for the same reason: a caller
        retries with one key because it means "the run I already asked for"; a tick means "whatever
        is new since last time", so a fixed key would dedupe every poll onto one run forever."""
        started: list[tuple[str, dict]] = []
        client = self._app(monkeypatch, started=started)

        client.post("/ingest-incremental-cron")
        client.post("/ingest-incremental-cron")

        assert len({run_id for run_id, _ in started}) == 2, "both ticks collapsed onto one run"

    def test_it_names_nobody_as_originator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No user fired this. An invented originator would put a row in an inbox belonging to
        whoever the literal happened to match."""
        started: list[tuple[str, dict]] = []
        client = self._app(monkeypatch, started=started)

        client.post("/ingest-incremental-cron")

        assert started[0][1].get("originator", "") == ""

    def test_an_unconfigured_deployment_ticks_harmlessly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        started: list[tuple[str, dict]] = []
        client = self._app(monkeypatch, started=started)
        monkeypatch.delenv("RASK_INGEST_CRON_KIND", raising=False)

        body = client.post("/ingest-incremental-cron").json()

        assert body["status"] == "SUCCESS"
        assert started == []

    def test_a_dispatch_failure_does_not_wedge_the_schedule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dapr retries a failed tick, so raising would re-fire the same poll against a source that
        has not changed. The truth lands in the run record and the graph, not in the binding."""
        client = self._app(monkeypatch, started=[])

        class _Broken:
            async def start(self, run_id: str, payload: dict) -> None:
                raise RuntimeError("engine down")

        client.app.state.workflow_starter = _Broken()

        body = client.post("/ingest-incremental-cron").json()

        assert body["status"] == "SUCCESS"
        assert "did not dispatch" in body["detail"]
