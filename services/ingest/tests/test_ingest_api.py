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

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ingest import create_app
from ingest.api import router
from ingest.runs import (
    SCHEDULE_TIMEOUT_SECONDS,
    InMemoryRunStore,
    RunRecord,
    is_redrivable,
    record_from_workflow_state,
    run_id_for,
)
from ingest.sources import register


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from service_kit.lakehouse.sources import SourceObject


class _RecordingStarter:
    """Records every workflow dispatch so A2 can assert that a duplicate starts ZERO.

    `on_dispatch` is the seam the failure tests drive, and it exists rather than having them rebind
    `start` itself: a plain function is not assignable to a method, so every such site carried a
    suppression AND the double stopped being checkably a `WorkflowStarter` at all. As a declared hook
    the class satisfies the real protocol and the swaps are ordinary typed assignments. A hook that
    raises pre-empts the record, exactly as replacing the method did.
    """

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, dict[str, Any]]] = []
        self.on_dispatch: Callable[[str, dict[str, object]], Awaitable[None]] | None = None

    async def start(self, run_id: str, payload: dict[str, object]) -> None:
        if self.on_dispatch is not None:
            await self.on_dispatch(run_id, payload)
        self.dispatched.append((run_id, dict(payload)))


class _NoUnits:
    """The adapter `test-src` builds: a real `SourceAdapter` that yields nothing.

    These tests exercise the accept handler, which never harvests — but `build` is declared
    `SourceFactory`, and a bare `object()` made the registration untypeable. An empty adapter is the
    honest double and costs one method.
    """

    def iter_objects(self) -> Iterator[SourceObject]:
        return iter(())


@pytest.fixture(autouse=True)
def _register_test_source() -> None:
    """A9 in miniature: adding a source is one adapter + one registry entry + one lineage twin."""
    if "test-src" not in __import__("ingest.sources", fromlist=["registered_kinds"]).registered_kinds():
        register(
            "test-src",
            build=lambda spec: _NoUnits(),
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


def test_the_dedupe_202_location_is_also_gettable(monkeypatch: pytest.MonkeyPatch) -> None:
    """ING-10: the dedupe branch sets its own Location and it must resolve too."""
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    app = create_app()
    app.state.workflow_starter = _RecordingStarter()
    c = TestClient(app)

    c.post("/api/ingests", json=BODY, headers={"Idempotency-Key": "dup"})
    dedupe = c.post("/api/ingests", json=BODY, headers={"Idempotency-Key": "dup"})
    assert dedupe.json()["deduplicated"] is True

    resolved = urljoin(str(dedupe.request.url), dedupe.headers["Location"])
    assert c.get(resolved).status_code == 200, f"dedupe Location {dedupe.headers['Location']!r} is not GETtable"


def test_a_keyless_call_is_REFUSED_rather_than_given_its_own_run(
    client: tuple[TestClient, _RecordingStarter, InMemoryRunStore],
) -> None:
    """This test used to assert the opposite, and the opposite was the defect.

    It read "token-less calls get distinct runs — there is no caller key to converge on", and pinned
    two unkeyed POSTs producing two runs. That is exactly what made this door unsafe behind a Dapr
    sidecar that replays 5xx: `key = idempotency_key or uuid.uuid4().hex` minted a fresh key per
    attempt, so a replayed 500 started a second ingest rather than converging on the first
    (open_fastapi-audit, the Dapr-retry finding).

    "There is no caller key to converge on" was the correct diagnosis and the wrong conclusion. The
    answer is to require one, not to invent one — a 422 naming the missing header is a better answer
    than a silently duplicated run. The door now refuses, and nothing is dispatched."""
    c, starter, _ = client
    first = c.post("/v1/ingests", json=BODY)
    second = c.post("/v1/ingests", json=BODY)
    assert first.status_code == 422, f"an unkeyed ingest was accepted: {first.text}"
    assert second.status_code == 422
    assert starter.dispatched == [], "a keyless call dispatched work before being refused"


# ── the 503's advice must be SATISFIABLE: a run the engine never took is re-drivable ──


def _failing_then_recording(starter: _RecordingStarter, failure: BaseException) -> None:
    """Make the NEXT dispatch fail with `failure` and every later one succeed normally."""
    attempts: list[int] = []

    async def _start(run_id: str, payload: dict[str, object]) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise failure

    starter.on_dispatch = _start


def test_a_run_the_engine_never_took_is_REDRIVEN_by_the_retry_the_503_advises(
    client: tuple[TestClient, _RecordingStarter, InMemoryRunStore],
) -> None:
    """THE zombie. The 503's own text was the thing that guaranteed the bug.

    The record was stored BEFORE the schedule call, so a failed dispatch still left a record — and the
    dedupe branch fired on the mere existence of one. The client did exactly what the detail told it
    to do, retried with the same Idempotency-Key, and got `deduplicated: true` back for a run no
    workflow engine had ever heard of. It had a record, it had a status, and nothing was driving it,
    for as long as the pod lived.

    So the assertion is on DISPATCH, not on the status code: a retry that answers 202 while starting
    nothing is the bug, not the fix.
    """
    c, starter, store = client
    _failing_then_recording(starter, TimeoutError())

    first = c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "zombie"})
    assert first.status_code == 503
    assert starter.dispatched == [], "nothing reached the engine, so nothing may be recorded as dispatched"

    second = c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "zombie"})

    assert second.status_code == 202
    assert second.json()["run_id"] == run_id_for("p1", "zombie")
    assert second.json()["deduplicated"] is False, "the retry started the run — reporting it as a duplicate is the zombie"
    assert len(starter.dispatched) == 1, "the advised retry did not re-schedule: the run has a record and no executor"

    record = asyncio.run(store.get(run_id_for("p1", "zombie")))
    assert record is not None
    assert record.scheduled is True


def test_a_dispatch_failure_that_is_NOT_retryable_leaves_no_ACCEPTED_run_behind(
    client: tuple[TestClient, _RecordingStarter, InMemoryRunStore],
) -> None:
    """A programming error keeps its 500 — but the record must not keep claiming ACCEPTED.

    The 500 is correct: a payload that will not serialize is this service's bug and no amount of
    retrying fixes it. What is not correct is leaving a run reporting a status nothing is executing,
    which is the same zombie the retryable path had, one exception type over.
    """
    c, starter, store = client

    async def _boom(run_id: str, payload: dict[str, object]) -> None:
        raise ValueError("payload is not serializable")

    starter.on_dispatch = _boom
    with pytest.raises(ValueError, match="not serializable"):
        c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "boom"})

    record = asyncio.run(store.get(run_id_for("p1", "boom")))
    assert record is not None
    assert record.status == "FAILED"
    assert "ValueError" in record.errors["dispatch"]
    assert record.scheduled is False


@pytest.mark.asyncio
async def test_a_duplicate_arriving_MID_DISPATCH_still_starts_nothing() -> None:
    """A2 under concurrency — the half a "re-drivable" state could have broken.

    "The dispatch never landed" and "the dispatch is happening right now on another request" look
    identical in the record unless the in-flight attempt is claimed, and treating the second as the
    first would race two dispatches of the same instance id to the engine. `dispatch_started_at` is
    that claim; it expires with the schedule call's own bound, so a request that dies mid-dispatch
    frees the run instead of stranding it.
    """
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.state.run_store = InMemoryRunStore()

    entered, release = asyncio.Event(), asyncio.Event()
    dispatched: list[str] = []

    class _SlowStarter:
        async def start(self, run_id: str, payload: dict[str, object]) -> None:
            dispatched.append(run_id)
            entered.set()
            await release.wait()

    app.state.workflow_starter = _SlowStarter()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ingest") as c:
        first = asyncio.create_task(c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "race"}))
        await asyncio.wait_for(entered.wait(), timeout=5)

        second = await c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "race"})
        release.set()
        assert (await first).status_code == 202

    assert second.status_code == 202
    assert second.json()["deduplicated"] is True
    assert len(dispatched) == 1, "a duplicate raced the in-flight dispatch to the engine"


@pytest.mark.asyncio
async def test_a_LATE_lease_release_cannot_erase_a_concurrent_dispatch() -> None:
    """A2 inverted, and permanently — the failure mode a blind `store.put` of a stale record has.

    The handler's copy of the record goes stale the moment it awaits the engine, so a slow attempt
    that later writes that copy back erases whatever a re-drive did in the meantime. The field it
    erases is `scheduled`, and `scheduled=False` is not self-correcting: the run the engine IS
    executing reads as re-drivable for the rest of the pod's life, so EVERY subsequent POST on that
    key dispatches it again. `dispatch_started_at` identifies the attempt, so an attempt that no
    longer holds the run writes nothing.
    """
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    store = InMemoryRunStore()
    app.state.run_store = store
    run_id = run_id_for("p1", "late")

    stalled = asyncio.Event()
    attempts: list[str] = []

    class _StalledThenFastStarter:
        async def start(self, rid: str, payload: dict[str, Any]) -> None:
            attempts.append(rid)
            if len(attempts) == 1:
                await stalled.wait()
                raise TimeoutError

    app.state.workflow_starter = _StalledThenFastStarter()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ingest") as c:
        first = asyncio.create_task(c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "late"}))
        while not attempts:
            await asyncio.sleep(0)

        # Age the first attempt's claim past the bound, exactly as its own three-second cap would.
        claimed = await store.get(run_id)
        assert claimed is not None
        await store.put(claimed.model_copy(update={"dispatch_started_at": datetime.now(UTC) - timedelta(seconds=SCHEDULE_TIMEOUT_SECONDS + 1)}))

        assert (await c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "late"})).status_code == 202
        stalled.set()
        assert (await first).status_code == 503

    settled = await store.get(run_id)
    assert settled is not None
    assert settled.scheduled is True, "the stalled attempt's lease release erased the re-drive's dispatch"
    assert is_redrivable(settled) is False, "a run the engine is executing reads as re-drivable — every retry re-dispatches it"


def _record(*, scheduled: bool = False, dispatch_started_at: datetime | None = None) -> RunRecord:
    return RunRecord(run_id="r", project="p", dataset="d", kind="test-src", scheduled=scheduled, dispatch_started_at=dispatch_started_at)


def test_the_dispatch_lease_expires_with_the_BOUND_that_created_it() -> None:
    """The lease cannot outlive the call it guards, or a dead request strands the run forever.

    A disconnected client unwinds the handler with `CancelledError`, which no except-branch catches —
    so an in-flight marker only a normal return could clear would recreate the zombie by another
    route. The schedule call is capped, so the cap IS the expiry.
    """
    assert is_redrivable(_record()) is True, "a record with no dispatch behind it is not a duplicate"
    assert is_redrivable(_record(dispatch_started_at=datetime.now(UTC))) is False
    assert is_redrivable(_record(dispatch_started_at=datetime.now(UTC) - timedelta(seconds=SCHEDULE_TIMEOUT_SECONDS + 1))) is True
    assert is_redrivable(_record(scheduled=True)) is False, "a scheduled run is the dedupe case, whatever its lease says"


def test_a_record_REBUILT_from_the_engine_counts_as_scheduled() -> None:
    """The engine holding the instance is the only evidence a dispatch landed — and it is enough.

    After a pod restart the store is empty and A3's rebuild is what keeps the run observable. If the
    rebuilt record defaulted to unscheduled, a POST with the same key would re-dispatch a run that is
    already executing — the mirror image of the zombie, and the worse direction of the two.
    """
    rebuilt = record_from_workflow_state("r9", {"serialized_input": json.dumps({"project": "demo", "dataset": "pages", "kind": "local-dir"})})

    assert rebuilt is not None
    assert rebuilt.scheduled is True
    assert is_redrivable(rebuilt) is False


# --------------------------------------------------------------------------- #
# Same key, different spec — a CONFLICT, on both branches
# --------------------------------------------------------------------------- #


def test_the_conflict_is_refused_on_the_REDRIVE_branch_too(client: tuple[TestClient, _RecordingStarter, InMemoryRunStore]) -> None:
    """The branch that was worse. A re-drivable record (nothing scheduled) would have been REPURPOSED
    onto the new spec — the run id keeps naming the first caller's request while the workflow ingests
    the second's. Checked before the branch, so both are covered by one guard."""
    c, starter, store = client

    asyncio.run(store.put(RunRecord(run_id=run_id_for("p1", "shared"), project="p1", dataset="pages", kind="test-src", scheduled=False)))

    res = c.post("/v1/ingests", json={**BODY, "kind": "test-src", "dataset": "other"}, headers={"Idempotency-Key": "shared"})

    assert res.status_code == 409, res.text
    assert starter.dispatched == [], "the re-drive branch dispatched a repurposed run"
