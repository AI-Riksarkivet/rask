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

from medallion.services.rayjobs_api_executor import RayJobsApiExecutor
from service_kit.lakehouse.executor import Capability, Executor


def test_the_adapter_satisfies_the_port() -> None:
    """Asked rather than assumed — the port is `runtime_checkable` precisely so this is a question.

    A partial implementation is refused here instead of raising at the first dispatch that reaches a
    missing method.
    """
    assert isinstance(RayJobsApiExecutor(), Executor)


def test_the_adapter_names_the_ray_engine() -> None:
    """`executor_for` resolves by name, so an adapter calling itself something else is unreachable."""
    from medallion.services.engine_names import RAY_ENGINE

    assert RayJobsApiExecutor().name == RAY_ENGINE


def test_the_adapter_claims_cancel_and_failure_detail_but_not_a_durable_record() -> None:
    """Capabilities are PROMISES the platform acts on, so an honest absence matters more than a full set.

    DURABLE_RECORD is deliberately absent: a Jobs-API submission lives in the head's GCS, and a head
    restart takes the job history with it — observed on this estate. Its absence is what licenses the
    resubmit machinery; claiming it would make `may_resubmit` refuse to resubmit a run that really was
    lost. CANCEL and FAILURE_DETAIL are claimed because `job_failure` classifies a real reason and the
    lane can delete a job by id.
    """
    caps = RayJobsApiExecutor().capabilities

    assert Capability.DURABLE_RECORD not in caps, "a Jobs-API job does not survive a head restart; claiming it would suppress a needed resubmit"
    assert Capability.CANCEL in caps
    assert Capability.FAILURE_DETAIL in caps


def test_the_adapter_refuses_a_task_registered_for_another_engine() -> None:
    """`validate_task` is the declaration-time half of the port, and refusing is its whole job."""
    import pytest

    from medallion.services.engine_names import IN_PROCESS_ENGINE
    from service_kit.lakehouse.executor import TaskRegistration

    with pytest.raises(Exception, match="inprocess"):
        RayJobsApiExecutor().validate_task(TaskRegistration(task="t", engine=IN_PROCESS_ENGINE, command="whatever"))


def test_the_registry_resolves_the_ray_engine_to_this_adapter() -> None:
    """The point of the adapter: `executor_for` stops being a function with one arm and zero callers."""
    from medallion.services.engine_names import RAY_ENGINE
    from medallion.services.engine_registry import executor_for, hosted_engines

    resolved = executor_for(RAY_ENGINE, storage_options={})

    assert isinstance(resolved, RayJobsApiExecutor)
    assert RAY_ENGINE in hosted_engines(), "the registry must report what it can now actually resolve"
