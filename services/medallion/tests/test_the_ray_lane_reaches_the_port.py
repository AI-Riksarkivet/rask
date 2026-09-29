"""The Ray lane is reachable through the `Executor` port, like every other engine.

[[LH-158]] / [[LH-159]] requirement 2. The port is declared in `service_kit.lakehouse.executor` and
`docs/DECISIONS.md` describes "a port, TWO adapters" — but until now NEITHER lane went through it:
`transform.py` hand-built `InProcessExecutor`, the Ray lane dispatched straight to the workflow, and
`executor_for` had zero production callers. A port nothing implements is a decision record describing
an architecture that does not exist.

WHAT THIS ADAPTER IS AND IS NOT. It is the COMPUTE engine's adapter — submit, status, failure, cancel
against the Ray Jobs REST API. It is NOT an orchestrator, and wrapping it does not remove Dapr
Workflow: the owner's BYO ruling separates the two axes, so the workflow engine orchestrates and calls
an executor, rather than being one. That composition is why this adapter can exist without the Ray
lane losing its durable run record.

IT WRAPS, IT DOES NOT REIMPLEMENT. Every method delegates to the functions the lane already uses
(`submit_or_reattach`, `job_status`, `job_failure`), because a second implementation of "what does a
Ray job's status mean" is exactly the drift `ray_jobs_api` was extracted to prevent — its own
docstring records a poller that watched an id the submitter never used.
"""

from __future__ import annotations

import pytest

from medallion.services.engine_names import IN_PROCESS_ENGINE, RAY_ENGINE
from medallion.services.inprocess_executor import InProcessExecutor
from medallion.services.rayjobs_api_executor import RayJobsApiExecutor


def test_the_adapter_refuses_a_task_registered_for_another_engine() -> None:
    """`validate_task` is the declaration-time half of the port, and refusing is its whole job."""
    from service_kit.lakehouse.executor import TaskRegistration

    with pytest.raises(Exception, match="inprocess"):
        RayJobsApiExecutor().validate_task(TaskRegistration(task="t", engine=IN_PROCESS_ENGINE, command="whatever"))


@pytest.mark.parametrize(
    ("engine", "adapter"),
    [
        pytest.param(RAY_ENGINE, RayJobsApiExecutor, id="ray"),
        # `engine_for` answers with a name; something must turn that name into the thing that runs it,
        # or the choice is advisory and the execution is hard-coded elsewhere.
        pytest.param(IN_PROCESS_ENGINE, InProcessExecutor, id="inprocess"),
    ],
)
def test_the_registry_resolves_each_engine_to_its_adapter(engine: str, adapter: type) -> None:
    """The point of the adapters: `executor_for` stops being a function with one arm and zero callers."""
    from medallion.services.engine_registry import executor_for, hosted_engines

    resolved = executor_for(engine, storage_options={})

    assert isinstance(resolved, adapter)
    assert resolved.name == engine
    assert engine in hosted_engines(), "the registry must report what it can now actually resolve"
