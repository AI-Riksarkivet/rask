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
    ScheduleUnavailable,
    is_redrivable,
    record_from_workflow_state,
    run_id_for,
)
from ingest.sources import SourceSpec, register


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator

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


def test_the_202_location_is_a_gettable_run_under_the_real_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """ING-10: the accepted run's `Location` must resolve to the run's GET, at any mount prefix.

    The header was a hardcoded `/v1/ingests/{id}` while the router mounts under `RASK_API_PREFIX`
    (`/api` in every deployment, `/api/v1` by code default) — so a client following the 202 Location
    404s, and behind the gateway (which rewrites the request path but not the Location) an absolute
    backend path is wrong again. A relative Location resolves against whatever public URL the caller
    actually hit. This test goes through the REAL app factory so the mount prefix is real, not the
    `/v1` the bare-router fixtures pin.
    """
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    app = create_app()
    starter = _RecordingStarter()
    app.state.workflow_starter = starter
    c = TestClient(app)

    res = c.post("/api/ingests", json=BODY, headers={"Idempotency-Key": "loc"})
    assert res.status_code == 202, res.text

    resolved = urljoin(str(res.request.url), res.headers["Location"])
    got = c.get(resolved)
    assert got.status_code == 200, f"Location {res.headers['Location']!r} -> {resolved} is not GETtable"
    assert got.json()["run_id"] == res.json()["run_id"]


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


def test_run_id_is_deterministic_across_processes() -> None:
    """The id derives from the CALLER's key, so a retry on another pod resolves the same run."""
    assert run_id_for("p1", "k") == run_id_for("p1", "k")
    assert run_id_for("p1", "k") != run_id_for("p2", "k")
    assert run_id_for("p1", "k") != run_id_for("p1", "other")


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
    """The re-drive keeps `created_at` and must name the spec it DISPATCHED — a status endpoint that
    points an operator at the wrong dataset is worse than one that 404s, because it looks like an
    answer.

    **The scenario this used to use is now unreachable, and that is the fix rather than a regression.**
    It drove a re-drive that carried a DIFFERENT dataset on the same Idempotency-Key, on the reasoning
    that only `project` feeds the run id so the change was "legitimate". It is not: that is one key
    naming two different requests, and it is now a 409 (see the conflict tests below). So the
    invariant is exercised where it still applies — a re-drive of the SAME request after a failed
    dispatch — and the divergence it guarded against can no longer be constructed at all.
    """
    c, starter, store = client
    _failing_then_recording(starter, TimeoutError())
    volume_a = {**BODY, "dataset": "volume-A"}

    assert c.post("/v1/ingests", json=volume_a, headers={"Idempotency-Key": "moved"}).status_code == 503
    assert c.post("/v1/ingests", json=volume_a, headers={"Idempotency-Key": "moved"}).status_code == 202

    record = asyncio.run(store.get(run_id_for("p1", "moved")))
    assert record is not None
    assert starter.dispatched[0][1]["dataset"] == "volume-A"
    assert record.dataset == "volume-A", "the record does not name the dataset that was dispatched"


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


# --------------------------------------------------------------------------- #
# GET /v1/ingests — only a REFUSAL filters a row
# --------------------------------------------------------------------------- #


def _governed_client(store: InMemoryRunStore) -> TestClient:
    """The router behind the SAME problem handlers the real app installs.

    The shared `client` fixture builds a bare `FastAPI()`, which is right for the handler-logic tests
    above — but these assert the WIRE STATUS a propagating domain error produces, and that mapping is
    `install_problem_handlers`' job (`ingest.__init__` calls it). Without it the exception escapes as
    a raw traceback and the test would be asserting the absence of a feature it never enabled.
    """
    from service_kit.lakehouse.ns_errors import install_problem_handlers

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    install_problem_handlers(app, __import__("logging").getLogger("test"))
    app.state.run_store = store
    app.state.workflow_starter = _RecordingStarter()
    return TestClient(app, raise_server_exceptions=False)


def _seeded(store: InMemoryRunStore) -> None:
    """Two runs in two projects, so a filter that drops everything is distinguishable from one that
    drops the right thing."""
    for run_id, project in (("r-mine", "p1"), ("r-theirs", "p2")):
        asyncio.run(store.put(RunRecord(run_id=run_id, project=project, dataset="pages", kind="test-src", status="RUNNING", created_at=datetime.now(UTC))))


@pytest.mark.parametrize(
    ("raised", "expected_status"),
    [
        # The one that legitimately filters a ROW: this caller may not see that project.
        ("permission", 200),
        # NOT a property of a row. The authorization layer is down, so EVERY record "filters" and the
        # list renders an outage as "you own nothing" — an answer, not an error. Must be a 503.
        ("unavailable", 503),
        # Also not a property of a row: the caller's own bearer is bad. Answering 200 with an empty
        # list tells them their token works.
        ("unauthenticated", 401),
    ],
)
def test_only_a_refusal_filters_a_row(
    raised: str,
    expected_status: int,
    client: tuple[TestClient, _RecordingStarter, InMemoryRunStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ingest import api as api_mod
    from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError

    _c, _starter, store = client
    _seeded(store)
    c = _governed_client(store)
    errors = {
        "permission": PermissionDeniedError("nope"),
        "unavailable": ServiceUnavailableError("authorization service is not available"),
        "unauthenticated": UnauthenticatedError("invalid token"),
    }

    # The PAGE door, not the per-row one: the listing resolves its distinct projects and asks once
    # (ING-05). `p2` is the unreadable project either way — a REFUSAL is now expressed by leaving it
    # out of the permitted set (that is what "filters a row" means at this seam), while the two
    # call-level faults still raise, which is exactly the distinction this test exists to pin.
    async def _authorize(_request: object, _settings: object, projects: Iterable[str] = (), *_a: object, **_k: object) -> frozenset[str]:
        named = set(projects)
        if raised == "permission":
            return frozenset(named - {"p2"})
        raise errors[raised]

    monkeypatch.setattr(api_mod, "authorize_ingest_projects", _authorize)

    res = c.get("/v1/ingests")

    assert res.status_code == expected_status, res.text
    if expected_status == 200:
        assert [r["run_id"] for r in res.json()["runs"]] == ["r-mine"], "the refusal must drop exactly the unreadable row"


def test_an_authz_outage_is_never_rendered_as_an_empty_list(
    client: tuple[TestClient, _RecordingStarter, InMemoryRunStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sharpest shape of the bug: EVERY project unreadable because the store is down. Before, that
    was a 200 with `{"runs": []}` — indistinguishable from a caller who has never ingested anything,
    and the one rendering an operator cannot act on."""
    from ingest import api as api_mod
    from lance_namespace import ServiceUnavailableError

    _c, _starter, store = client
    _seeded(store)
    c = _governed_client(store)

    async def _down(*_a: object, **_k: object) -> None:
        raise ServiceUnavailableError("openfga unreachable")

    monkeypatch.setattr(api_mod, "authorize_ingest_projects", _down)

    res = c.get("/v1/ingests")

    assert res.status_code == 503, res.text
    assert res.json() != {"runs": []}


# --------------------------------------------------------------------------- #
# Same key, different spec — a CONFLICT, on both branches
# --------------------------------------------------------------------------- #


def test_the_same_key_with_a_different_spec_is_a_conflict(client: tuple[TestClient, _RecordingStarter, InMemoryRunStore]) -> None:
    """An Idempotency-Key means "this exact request". Only `project` and the key go into the run id,
    so reusing one key for a different dataset used to land on the FIRST run and answer
    `deduplicated=true` — telling the caller a request was accepted that was never dispatched."""
    c, starter, _store = client

    first = c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "shared"})
    assert first.status_code in {200, 202}, first.text
    dispatched_before = len(starter.dispatched)

    second = c.post("/v1/ingests", json={**BODY, "dataset": "something-else"}, headers={"Idempotency-Key": "shared"})

    assert second.status_code == 409, second.text
    assert "something-else" in second.json()["detail"]
    assert len(starter.dispatched) == dispatched_before, "a conflicting spec must dispatch nothing"


def test_the_conflict_is_refused_on_the_REDRIVE_branch_too(client: tuple[TestClient, _RecordingStarter, InMemoryRunStore]) -> None:
    """The branch that was worse. A re-drivable record (nothing scheduled) would have been REPURPOSED
    onto the new spec — the run id keeps naming the first caller's request while the workflow ingests
    the second's. Checked before the branch, so both are covered by one guard."""
    c, starter, store = client

    asyncio.run(store.put(RunRecord(run_id=run_id_for("p1", "shared"), project="p1", dataset="pages", kind="test-src", scheduled=False)))

    res = c.post("/v1/ingests", json={**BODY, "kind": "test-src", "dataset": "other"}, headers={"Idempotency-Key": "shared"})

    assert res.status_code == 409, res.text
    assert starter.dispatched == [], "the re-drive branch dispatched a repurposed run"


def test_the_same_key_with_the_SAME_spec_still_dedupes(client: tuple[TestClient, _RecordingStarter, InMemoryRunStore]) -> None:
    """The guard must not break A2. Identical request, identical key → one dispatch, deduplicated."""
    c, starter, _store = client

    c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "same-spec"})
    again = c.post("/v1/ingests", json=BODY, headers={"Idempotency-Key": "same-spec"})

    assert again.status_code in {200, 202}, again.text
    assert again.json()["deduplicated"] is True
    assert len(starter.dispatched) == 1, "the dedupe stopped working"
