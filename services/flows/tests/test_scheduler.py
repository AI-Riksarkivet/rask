"""What the scheduler concludes from a schedule call that does not answer — the double-execution rule.

`DaprFlowScheduler.schedule` is the seam `routes.create_run` degrades on, so the question it answers
is not "did the call return?" but "does the instance EXIST?". Those came apart because
`asyncio.wait_for` cancels the WAIT and never the work: `asyncio.to_thread` hands the synchronous
gRPC `schedule_new_workflow` to an executor with no cancellation channel, so a slow-but-successful
schedule raised `TimeoutError` at exactly the moment the instance was being created — and the route
read that as "the durable lane is unavailable" and ran the same graph inline. One run, two
executions, and the caller told about the inline one.

The suite subclasses the real scheduler and substitutes only `_create` / `_exists`, the two methods
that need a sidecar. Everything under test — the shield, the settle window, the probe, and which of
the three outcomes each combination produces — is the shipped code, because the ordering is the part
that was wrong and a double that reimplemented it would prove nothing.
"""

import asyncio
import inspect
import threading
import time
from collections.abc import Iterator

import pytest

from flows import lifespan
from flows.dependencies import ScheduleUnconfirmed


class _Engine(lifespan.DaprFlowScheduler):
    """A sidecar that can be slow, can refuse, and can lose its own state store.

    `_create` sleeps BEFORE recording the instance, which is the shape that matters: during the sleep
    the instance genuinely does not exist yet, so a probe answering "absent" is answering honestly
    and is still the wrong thing to act on.
    """

    def __init__(
        self,
        *,
        create_delay: float = 0.0,
        create_error: Exception | None = None,
        probe_error: Exception | None = None,
        probe_block: threading.Event | None = None,
        creates: bool = True,
    ) -> None:
        self.create_delay = create_delay
        self.create_error = create_error
        self.probe_error = probe_error
        self.probe_block = probe_block
        self.creates = creates
        self.instances: set[str] = set()
        self.probes = 0

    def _create(self, run_id: str, payload: dict[str, object]) -> None:
        time.sleep(self.create_delay)
        if self.create_error is not None:
            raise self.create_error
        if self.creates:
            self.instances.add(run_id)

    def _exists(self, run_id: str) -> bool:
        self.probes += 1
        if self.probe_block is not None:
            self.probe_block.wait(5.0)
        if self.probe_error is not None:
            raise self.probe_error
        return run_id in self.instances


@pytest.fixture(autouse=True)
def _fast_bounds() -> Iterator[None]:
    """Milliseconds instead of seconds. The constants are read at call time from the module, so this
    scales the real policy rather than replacing it."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lifespan, "SCHEDULE_TIMEOUT_SECONDS", 0.05)
        mp.setattr(lifespan, "SETTLE_TIMEOUT_SECONDS", 0.05)
        mp.setattr(lifespan, "PROBE_TIMEOUT_SECONDS", 0.5)
        yield


@pytest.mark.asyncio
async def test_a_slow_but_successful_schedule_is_not_reported_as_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE double-execution defect. The call outruns its bound and then succeeds; the instance
    exists, so `schedule` must return and the route must never reach its inline lane."""
    monkeypatch.setattr(lifespan, "SETTLE_TIMEOUT_SECONDS", 2.0)
    engine = _Engine(create_delay=0.2)

    await engine.schedule("run-slow", {})  # must not raise — a TimeoutError here runs the graph twice

    assert engine.instances == {"run-slow"}


@pytest.mark.asyncio
async def test_a_call_still_in_flight_is_unconfirmed_rather_than_failed() -> None:
    """Neither started nor refused: the honest answer is "unknown", which the route turns into a 503
    the caller can retry with the same key. Degrading here is a coin flip whose losing side executes
    the graph twice — and the tail of this test shows the losing side is real, because the instance
    the request gave up on lands a moment later."""
    # A second against a 0.1s policy: the margin is deliberate, so a loaded runner cannot turn this
    # into the "landed late" case and quietly stop testing the branch it is named for.
    engine = _Engine(create_delay=1.0)

    with pytest.raises(ScheduleUnconfirmed, match="neither started nor refused run run-flight"):
        await engine.schedule("run-flight", {})
    assert engine.probes == 1  # it ASKED before refusing

    for _ in range(30):
        if engine.instances:
            break
        await asyncio.sleep(0.1)
    assert engine.instances == {"run-flight"}


@pytest.mark.asyncio
async def test_an_error_is_swallowed_when_the_engine_says_the_instance_exists() -> None:
    """A retried POST derives the SAME instance id, which the engine refuses as a duplicate. That
    refusal is the dedupe working, not a failure — re-running it inline would be the second
    execution the deterministic id exists to prevent."""
    engine = _Engine(create_error=RuntimeError("instance already exists"))
    engine.instances.add("run-dup")

    await engine.schedule("run-dup", {})

    assert engine.probes == 1


@pytest.mark.asyncio
async def test_a_refusal_the_engine_confirms_is_re_raised_so_the_route_can_degrade() -> None:
    """The measured 2026-08-06 incident: a sidecar was injected but `lance-statestore` was not scoped
    to app-id `flows`, so the create raised and NOTHING was created. Degrading to the inline lane
    there is deliberate and must survive — the graph is perfectly runnable in-process."""
    refusal = RuntimeError("the state store is not configured to use the actor runtime")
    engine = _Engine(create_error=refusal, creates=False)

    with pytest.raises(RuntimeError) as caught:
        await engine.schedule("run-nostore", {})

    assert caught.value is refusal  # the ORIGINAL cause, so the log names the component to fix
    assert engine.probes == 1


@pytest.mark.asyncio
async def test_a_probe_that_cannot_reach_the_sidecar_still_degrades() -> None:
    """No engine to ask means no engine that could have created anything. The probe failing is the
    same evidence the schedule failure already gave, so the inline lane stays reachable on a sidecar
    that is not answering at all."""
    refusal = RuntimeError("connection refused")
    engine = _Engine(create_error=refusal, creates=False, probe_error=OSError("no sidecar"))

    with pytest.raises(RuntimeError) as caught:
        await engine.schedule("run-nosidecar", {})

    assert caught.value is refusal  # not the probe's error: the caller needs the reason it failed


def test_the_two_substituted_methods_call_the_sdk_the_way_the_sdk_is_spelled() -> None:
    """Every test above replaces `_create` and `_exists`, so nothing else in this suite would notice
    the SDK moving under them — and the probe is the half whose FAILURE is silent by design (an
    unreachable engine answers "absent"). A signature check is two lines and closes that whole class,
    the same argument `test_workflow.py`'s registration test makes.
    """
    import dapr.ext.workflow as wf

    create = inspect.signature(wf.DaprWorkflowClient.schedule_new_workflow).parameters
    assert {"workflow", "input", "instance_id"} <= set(create)

    probe = inspect.signature(wf.DaprWorkflowClient.get_workflow_state).parameters
    assert "instance_id" in probe
    assert "fetch_payloads" in probe
    assert probe["fetch_payloads"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_a_probe_that_hangs_cannot_outlive_its_own_bound() -> None:
    """The probe adjudicates a wait; a probe that can hang longer than that wait just moves the
    problem. Bounded, and a bound that fires answers "absent" like any other unreachable probe."""
    block = threading.Event()
    engine = _Engine(create_error=RuntimeError("nope"), creates=False, probe_block=block)

    started = time.perf_counter()
    try:
        with pytest.raises(RuntimeError, match="nope"):
            await engine.schedule("run-hang", {})
        assert time.perf_counter() - started < 2.0
    finally:
        block.set()  # release the worker thread rather than leaving it to the loop's shutdown
