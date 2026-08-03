"""A1, A2, A8 — the declared semantics, each pinned by the test that exercises it.

open_ingest.md §3.4 names the estate's recurring disease: a contract declared and its semantics
absent — `202 Accepted` on a synchronous handler, an `Idempotency-Key` that deduplicates nothing.
The rule for the new plane is that no contract ships without the test that exercises it, so these
assert BEHAVIOUR (no second workflow dispatched) rather than shape (the ids happen to match).

The workflow starter is a structural fake, following the estate's own pattern for an external
client (`_FakeRayClient` in test_pipelines_registry.py): it records dispatches so a test can assert
on them. That is a boundary double, not a mocked-away assertion — the thing under test is the
handler's logic, and a live sidecar would prove nothing extra about it.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ingest.api import router
from ingest.runs import InMemoryRunStore, RunRecord, run_id_for
from ingest.sources import SourceSpec, register


class _RecordingStarter:
    """Records every workflow dispatch so A2 can assert that a duplicate starts ZERO."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, dict[str, Any]]] = []

    async def start(self, run_id: str, payload: dict[str, object]) -> None:
        self.dispatched.append((run_id, dict(payload)))


@pytest.fixture(autouse=True)
def _register_test_source() -> None:
    """A9 in miniature: adding a source is one adapter + one registry entry + one lineage twin."""
    if "test-src" not in __import__("ingest.sources", fromlist=["registered_kinds"]).registered_kinds():
        register(
            "test-src",
            build=lambda spec: object(),  # type: ignore[arg-type,return-value]
            lineage_input=lambda spec: __import__("ingest.sources", fromlist=["LineageInput"]).LineageInput(namespace="test", name=spec.dataset),
        )


@pytest.fixture
def client() -> tuple[TestClient, _RecordingStarter, InMemoryRunStore]:
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    store = InMemoryRunStore()
    starter = _RecordingStarter()
    app.state.run_store = store
    app.state.workflow_starter = starter
    return TestClient(app), starter, store


BODY = {"kind": "test-src", "project": "p1", "dataset": "pages", "options": {}}


def test_a1_accept_returns_202_without_doing_the_work(
    client: tuple[TestClient, _RecordingStarter, InMemoryRunStore],
) -> None:
    """A1: 202 in well under a second, and the work proceeds after the response.

    The medallion's head returned 202 only after a sequential per-page harvest. Here the handler
    mints identity, dispatches, and returns — so the elapsed time is request handling, not ingest.
    """
    c, starter, _ = client
    started = time.perf_counter()
    res = c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "k1"})
    elapsed = time.perf_counter() - started

    assert res.status_code == 202
    assert elapsed < 1.0, f"accept took {elapsed:.3f}s — the handler is doing the work"
    assert res.json()["status"] == "ACCEPTED"
    assert res.headers["Location"].endswith(res.json()["run_id"])
    assert len(starter.dispatched) == 1, "the run must be dispatched, not performed inline"


def test_a2_same_idempotency_key_starts_no_second_workflow(
    client: tuple[TestClient, _RecordingStarter, InMemoryRunStore],
) -> None:
    """A2: same key + same spec -> the same run resource and ZERO new unit work.

    This is the assertion the medallion could not have passed: it converged the run id and
    re-harvested the volume regardless. Asserting on dispatch count is what makes the difference
    visible — matching ids alone would have passed there too.
    """
    c, starter, _ = client
    first = c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "same"})
    second = c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "same"})

    assert first.json()["run_id"] == second.json()["run_id"]
    assert second.json()["deduplicated"] is True
    assert len(starter.dispatched) == 1, "a repeated Idempotency-Key started a second workflow"


def test_run_id_is_deterministic_across_processes() -> None:
    """The id derives from the CALLER's key, so a retry on another pod resolves the same run."""
    assert run_id_for("p1", "k") == run_id_for("p1", "k")
    assert run_id_for("p1", "k") != run_id_for("p2", "k")
    assert run_id_for("p1", "k") != run_id_for("p1", "other")


def test_missing_key_does_not_collide_across_callers(
    client: tuple[TestClient, _RecordingStarter, InMemoryRunStore],
) -> None:
    """Token-less calls get distinct runs — there is no caller key to converge on."""
    c, starter, _ = client
    a = c.post("/v1/ingests", json=BODY)
    b = c.post("/v1/ingests", json=BODY)
    assert a.json()["run_id"] != b.json()["run_id"]
    assert len(starter.dispatched) == 2


def test_unknown_source_kind_is_refused_loudly(
    client: tuple[TestClient, _RecordingStarter, InMemoryRunStore],
) -> None:
    """I1: an unregistered kind refuses with the kinds that exist — it never falls through."""
    c, starter, _ = client
    res = c.post("/v1/ingests", json={**BODY, "kind": "nope"}, headers={"Idempotency-Key": "x"})
    assert res.status_code == 400
    assert "nope" in res.json()["detail"]
    assert starter.dispatched == []


@pytest.mark.asyncio
async def test_a8_complete_without_lineage_renders_as_a_defect() -> None:
    """A8: 'a green sync with no lineage edge is a bug the UI should surface, not report green'."""
    healthy = RunRecord(run_id="r", project="p", dataset="d", kind="test-src", status="COMPLETE", lineage_run_present=True)
    holed = RunRecord(run_id="r", project="p", dataset="d", kind="test-src", status="COMPLETE", lineage_run_present=False)
    assert healthy.is_defective is False
    assert holed.is_defective is True


def test_status_surfaces_the_defect_state(
    client: tuple[TestClient, _RecordingStarter, InMemoryRunStore],
) -> None:
    c, _, store = client
    import asyncio

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        store.put(
            RunRecord(
                run_id="r1",
                project="p",
                dataset="d",
                kind="test-src",
                status="COMPLETE",
                lineage_run_present=False,
            )
        )
    )
    res = c.get("/v1/ingests/r1")
    assert res.status_code == 200
    assert "no lineage run exists" in res.json()["defect"]


def test_unknown_run_is_404(client: tuple[TestClient, _RecordingStarter, InMemoryRunStore]) -> None:
    c, _, _ = client
    assert c.get("/v1/ingests/nope").status_code == 404


def test_source_spec_carries_no_dataset_path() -> None:
    """I2: the caller names {project, dataset}; the catalog resolves where that is.

    A fixed per-lane URI is precisely why ingesting volume B overwrote volume A.
    """
    spec = SourceSpec(kind="test-src", project="p", dataset="pages")
    assert not any("://" in str(v) for v in spec.model_dump().values()), "a dataset PATH leaked into the spec"
