"""`_DaprWorkflowStarter.start` — the seam where a sidecar exception becomes the door's vocabulary.

WHY THIS FILE EXISTS. F8's fix maps a non-timeout sidecar failure onto a retryable 503 instead of a
raw 500, and the classification deliberately lives HERE rather than in `api.py`, which knows nothing
about gRPC. But the reviewer of that fix noted the gap plainly: `_sidecar_error_types()` had a test
of its own TYPE SET, and `api.py` had a test of its MAPPING — driven by a fake starter raising
`ScheduleUnavailable` directly — so nothing exercised the code that actually decides which exceptions
become that class. The whole point of F8 rested on an untested branch.

These tests drive the real `start()` with a fake workflow client, so every arm of its except-block is
executed: converge, classify-as-retryable, and re-raise-as-a-bug.

The three arms are not interchangeable, and getting one wrong is expensive in a specific way:
  * converge wrongly  -> two workflows harvest one volume,
  * classify wrongly  -> a permanent misconfiguration becomes an infinite client retry loop,
  * re-raise wrongly  -> an operator's transport blip is reported as this service's bug.
"""

import asyncio
from typing import Any

import pytest

from ingest import ScheduleUnavailable, _DaprWorkflowStarter, _is_already_scheduled, _sidecar_error_types


class _Boom:
    """A fake `DaprWorkflowClient` whose `schedule_new_workflow` raises what the test asks for."""

    def __init__(self, error: BaseException | None) -> None:
        self._error = error
        self.calls = 0

    def schedule_new_workflow(self, **_: Any) -> None:
        self.calls += 1
        if self._error is not None:
            raise self._error


@pytest.fixture
def drive(monkeypatch: pytest.MonkeyPatch):
    """Return a callable that runs `start()` against a client raising `error`, and yields the client.

    Patches `dapr.ext.workflow.DaprWorkflowClient` rather than the starter, so the code under test is
    the SHIPPED body — the lazy import inside `start` resolves to the patched attribute.
    """
    import dapr.ext.workflow as wf

    def _drive(error: BaseException | None) -> _Boom:
        client = _Boom(error)
        monkeypatch.setattr(wf, "DaprWorkflowClient", lambda *a, **k: client)
        asyncio.run(_DaprWorkflowStarter().start("run-1", {"run_id": "run-1"}))
        return client

    return _drive


def test_a_transport_error_becomes_the_retryable_class(drive) -> None:
    """The F8 case: the sidecar answered badly, so the caller may retry.

    `ConnectionRefusedError` is an `OSError`, which `_sidecar_error_types()` includes precisely for
    "the socket died under the channel" — daprd not up yet is the shape this door was reopened for.
    """
    with pytest.raises(ScheduleUnavailable):
        drive(ConnectionRefusedError("daprd is not listening yet"))


def test_the_retryable_class_carries_the_original_as_its_cause(drive) -> None:
    """`raise ... from exc`. Without the cause an operator gets a 503 and no way to learn WHY."""
    original = ConnectionRefusedError("connection refused")
    with pytest.raises(ScheduleUnavailable) as caught:
        drive(original)
    assert caught.value.__cause__ is original


def test_a_PROGRAMMING_error_is_re_raised_unchanged(drive) -> None:
    """The boundary that makes the classifier safe: this service's own bug keeps its 500.

    A payload that will not serialize, a name that does not resolve — none of these get better on a
    retry, and turning them into a 503 is how a permanent misconfiguration becomes an infinite client
    retry loop. Asserting `ValueError` (not `ScheduleUnavailable`) is the whole point.
    """
    with pytest.raises(ValueError, match="not JSON serializable"):
        drive(ValueError("not JSON serializable"))


def test_an_ALREADY_EXISTS_refusal_CONVERGES_instead_of_raising(drive) -> None:
    """Not a failure. `instance_id` is deterministic, so the instance the engine names IS this run.

    Reachable by design: `asyncio.wait_for` cancels the await, never the thread behind `to_thread`,
    so a schedule that was merely slow still lands — and the retry the 503 advises then meets its own
    earlier dispatch. Raising here would make that advice impossible to satisfy; dispatching past it
    would harvest the volume twice.
    """
    drive(RuntimeError("instance with ID 'run-1' already exists"))  # must NOT raise


def test_the_converge_match_is_case_insensitive_and_substring(drive) -> None:
    """Pin the matcher's actual contract — it is message-based because no typed error is exported."""
    drive(RuntimeError("Workflow ALREADY EXISTS for that id"))  # must NOT raise
    assert _is_already_scheduled(RuntimeError("already exists"))
    assert not _is_already_scheduled(RuntimeError("connection refused"))


def test_a_TIMEOUT_is_never_re_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    """`TimeoutError` IS a subclass of `OSError`, so it is structurally inside `_sidecar_error_types()`.

    The `except TimeoutError: raise` arm precedes the classifier for exactly this reason — a decided
    outcome must not be put at the mercy of whatever text the exception happens to carry. If the
    ordering is ever swapped, this test fails and the door starts reporting its own bound as a
    sidecar fault.
    """
    assert issubclass(TimeoutError, tuple(t for t in _sidecar_error_types() if isinstance(t, type)))

    import dapr.ext.workflow as wf

    monkeypatch.setattr(wf, "DaprWorkflowClient", lambda *a, **k: _Boom(TimeoutError("slow")))
    with pytest.raises(TimeoutError):
        asyncio.run(_DaprWorkflowStarter().start("run-1", {"run_id": "run-1"}))


def test_a_clean_schedule_raises_nothing_and_calls_the_engine_once(drive) -> None:
    """The happy path, and the guard against a fix that accidentally swallows every outcome."""
    client = drive(None)
    assert client.calls == 1
