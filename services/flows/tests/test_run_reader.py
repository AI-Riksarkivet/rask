"""A durable run must be READABLE, not just schedulable.

THE DEFECT. `app.state.runs` is written by `_remember` and read by `get_run`, and in the durable lane
the only thing ever written is `RunState(status="running")` with an empty `nodes` map — nothing
updates it when the workflow finishes. So `GET /flows/runs/{id}` reported a COMPLETED run as running
forever, never surfaced a FAILED run's error, dropped the record once `max_runs` newer runs arrived,
and 404'd every prior run after a pod restart — while the workflow itself was executing, or had long
since finished, perfectly well. The durable lane was write-only.

The engine already holds the answer: `flow_run_workflow` RETURNS `RunState.model_dump()`, so the
instance's `serialized_output` IS the run document. These tests drive `_state_from_engine`, the pure
mapping from an instance's state dict to a `RunState`, because that is where every branch lives and
it needs no sidecar to exercise.
"""

import pytest

from flows.models import RunState
from flows.routes import _state_from_engine


RUN_ID = "run-abc"


def test_a_completed_run_reads_back_as_what_it_returned() -> None:
    """The regression. Pre-fix this run reported `running` for the rest of the pod's life."""
    finished = RunState(run_id=RUN_ID, status="succeeded").model_dump_json()

    state = _state_from_engine(RUN_ID, {"runtime_status": "COMPLETED", "serialized_output": finished})

    assert state is not None
    assert state.status == "succeeded", "a completed workflow still read as running — the defect"


def test_a_run_the_workflow_itself_called_failed_stays_failed() -> None:
    """The workflow decides succeeded-vs-failed by inspecting its own node results. The reader must
    carry that verdict rather than form a second opinion from `runtime_status` — a workflow that
    COMPLETED while its nodes failed is a failed run, and the two facts disagree by design."""
    returned = RunState(run_id=RUN_ID, status="failed", error="node b failed").model_dump_json()

    state = _state_from_engine(RUN_ID, {"runtime_status": "COMPLETED", "serialized_output": returned})

    assert state is not None
    assert (state.status, state.error) == ("failed", "node b failed")


@pytest.mark.parametrize("status", ["FAILED", "TERMINATED"])
def test_an_instance_that_never_returned_is_failed_with_a_reason(status: str) -> None:
    """FAILED/TERMINATED produced no `RunState` — the body raised or was killed — so this is the one
    branch that must compose the answer, and it must still say something an operator can act on."""
    state = _state_from_engine(RUN_ID, {"runtime_status": status, "failure_details": "boom"})

    assert state is not None
    assert state.status == "failed"
    assert state.error is not None and state.error != ""


@pytest.mark.parametrize("status", ["RUNNING", "PENDING", "SUSPENDED"])
def test_a_live_instance_reports_running(status: str) -> None:
    state = _state_from_engine(RUN_ID, {"runtime_status": status})

    assert state is not None
    assert state.status == "running"


def test_no_engine_answer_is_None_so_the_caller_can_fall_back() -> None:
    """ "The engine has nothing" and "the engine says running" are different facts, and only the first
    may fall back to the local record. Collapsing them would make a 404 unreachable."""
    assert _state_from_engine(RUN_ID, None) is None
    assert _state_from_engine(RUN_ID, {}) is None


def test_a_completed_run_with_no_output_is_reported_not_swallowed() -> None:
    """Boundary: COMPLETED with nothing to parse. Falling back to the local record here would report
    a finished run as `running` — the exact defect, reintroduced through the error path."""
    state = _state_from_engine(RUN_ID, {"runtime_status": "COMPLETED", "serialized_output": ""})

    assert state is not None
    assert state.status == "failed"


def test_unparseable_output_does_not_read_as_a_missing_run() -> None:
    """A completed instance whose output will not parse is a contract break between the reader and
    the workflow, not an absent run. Returning None would send an operator looking for a run that
    plainly exists."""
    state = _state_from_engine(RUN_ID, {"runtime_status": "COMPLETED", "serialized_output": "{not json"})

    assert state is not None
    assert state.status == "failed"
