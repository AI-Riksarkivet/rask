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

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ingest.api import router
from ingest.runs import (
    SCHEDULE_TIMEOUT_SECONDS,
    InMemoryRunStore,
    RunRecord,
    ScheduleUnavailable,
    is_redrivable,
    record_from_workflow_state,
    run_id_for,
)
from ingest.sources import SourceSpec, register


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

    asyncio.run(
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


def test_a_busy_workflow_engine_is_a_RETRYABLE_503_not_a_500(client: tuple[TestClient, _RecordingStarter, InMemoryRunStore]) -> None:
    """A1's bound must not turn a slow sidecar into an unretryable error.

    Bounding the schedule call is what keeps 202-under-a-second a contract rather than a hope. But
    the first version let the TimeoutError escape, and FastAPI answered 500 — observed in-cluster on
    a pod whose daprd had only just started. 500 tells a client "this request can never work"; the
    truth is "the engine was busy, ask again", which is a 503 with a Retry-After.

    The run id is deterministic, so the retry converges on the SAME run rather than starting a second
    one — which is the property that makes advising a retry safe at all.
    """
    c, starter, _ = client

    async def _timeout(run_id: str, payload: dict[str, object]) -> None:
        raise TimeoutError

    starter.on_dispatch = _timeout
    res = c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "busy"})

    assert res.status_code == 503
    assert res.headers.get("Retry-After")
    assert "Idempotency-Key" in res.json()["detail"]


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


def test_a_NON_TIMEOUT_sidecar_failure_is_the_same_retryable_503(
    client: tuple[TestClient, _RecordingStarter, InMemoryRunStore],
) -> None:
    """Mapping only `TimeoutError` left every other sidecar failure a raw 500 on a stored record.

    A refused gRPC channel, a state store not configured for the actor runtime, a sidecar that has not
    finished starting — all of them are "ask again", and all of them answered "this can never work"
    while stranding the run exactly as the timeout path did. Same class, same answer, same re-drive.
    """
    c, starter, _ = client
    _failing_then_recording(starter, ScheduleUnavailable("the state store is not configured to use the actor runtime"))

    first = c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "sidecar"})

    assert first.status_code == 503
    assert first.headers.get("Retry-After")
    assert "Idempotency-Key" in first.json()["detail"]
    assert "actor runtime" in first.json()["detail"], "the operator-actionable reason must survive into the 503"

    assert c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "sidecar"}).status_code == 202
    assert len(starter.dispatched) == 1


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


def test_a_REDRIVE_records_the_spec_it_actually_DISPATCHES(
    client: tuple[TestClient, _RecordingStarter, InMemoryRunStore],
) -> None:
    """The re-drive keeps `created_at`; it must NOT keep the abandoned attempt's spec.

    Only `project` feeds the run id, so a retry on the same Idempotency-Key can legitimately carry a
    different dataset or kind — and the workflow is dispatched with the BODY. A record that reused the
    first attempt's fields then named a table this run never writes to, while the harvest landed
    somewhere else entirely: a status endpoint that points an operator at the wrong dataset is worse
    than one that 404s, because it looks like an answer.
    """
    c, starter, store = client
    _failing_then_recording(starter, TimeoutError())
    volume_a = {**BODY, "dataset": "volume-A"}
    volume_b = {**BODY, "dataset": "volume-B"}

    assert c.post("/v1/ingests", json=volume_a, headers={"Idempotency-Key": "moved"}).status_code == 503
    assert c.post("/v1/ingests", json=volume_b, headers={"Idempotency-Key": "moved"}).status_code == 202

    record = asyncio.run(store.get(run_id_for("p1", "moved")))
    assert record is not None
    assert starter.dispatched[0][1]["dataset"] == "volume-B"
    assert record.dataset == "volume-B", "the record names the dataset the ABANDONED attempt asked for"


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


def test_the_adapter_converges_on_an_instance_the_engine_ALREADY_holds() -> None:
    """`wait_for` cancels the await, never the thread — so a timed-out schedule can still have landed.

    The retry then meets its own earlier dispatch. Treating that as a failure would make the 503's
    advice impossible to satisfy; dispatching past it would run the harvest twice.
    """
    from ingest import _is_already_scheduled

    assert _is_already_scheduled(RuntimeError("an active workflow with ID 'r9' already exists")) is True
    assert _is_already_scheduled(RuntimeError("failed to connect to sidecar: connection refused")) is False


def test_only_a_TRANSPORT_failure_is_classified_retryable() -> None:
    """The line between "ask again" and "this service is broken".

    Type-based, not status-code-based: guessing which gRPC codes are transient is how a permanent
    misconfiguration becomes an infinite client retry loop.
    """
    from ingest import _sidecar_error_types

    retryable = _sidecar_error_types()
    assert isinstance(ConnectionRefusedError("no sidecar"), retryable)
    assert not isinstance(ValueError("payload is not serializable"), retryable)
    assert not isinstance(TypeError("bad signature"), retryable)
