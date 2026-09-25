"""The Ray lane as an `Executor` — the compute engine behind the port, not an orchestrator.

[[LH-158]] THE PORT EXISTED AND NOTHING IMPLEMENTED IT. `service_kit.lakehouse.executor.Executor` is
declared, `docs/DECISIONS.md` describes "a port, TWO adapters", and until this landed neither lane went
through it: `transform.py` hand-built `InProcessExecutor`, the Ray lane dispatched straight to the
workflow, and `engine_registry.executor_for` had zero production callers. A port nothing implements is
a decision record describing an architecture that does not exist, which is the condition the estate's
own rule about falsified prose exists to prevent.

**THIS IS THE COMPUTE ENGINE'S ADAPTER, AND IT DOES NOT REPLACE DAPR WORKFLOW.** The owner's BYO ruling
(2026-09-15) separates two axes — bring your own WORKFLOW engine, bring your own COMPUTE engine — so
the workflow orchestrates and CALLS an executor rather than being one. That composition is why this
adapter can exist without the Ray lane losing the durable run record the workflow gives it.

**IT WRAPS; IT DOES NOT REIMPLEMENT.** Every method delegates to the functions the lane already uses —
`submit_or_reattach`, `job_status`, `job_failure`. A second implementation of "what does a Ray job's
status mean" is precisely the drift `ray_jobs_api` was extracted to prevent: its own docstring records
a poller that watched an id the submitter never used and reported a healthy job as missing forever.

**WHY `DURABLE_RECORD` IS NOT CLAIMED**, which is the one capability judgement here. A Jobs-API
submission lives in the head's GCS, and a head restart takes the job history with it — observed on this
estate when the in-cluster head was restarted. Its ABSENCE is what licenses the resubmit machinery, so
claiming it would tell a resubmitting caller that a run which really was lost is still held. Flyte's
RayJob CR would carry it, which is the one property that path genuinely has and this one does not.
"""

from __future__ import annotations

from typing import Any

import httpx

from medallion.services import ray_jobs_api
from medallion.services.engine_names import RAY_ENGINE
from service_kit.lakehouse.executor import Capability, RunFailure, RunHandle, RunState, SubmitOutcome, TaskRegistration, WrongEngineError
from service_kit.lakehouse.work_order import WorkOrder


#: POSIX SIGKILL. Portable across engines, unlike a message string — on this lane it is the host-RAM
#: OOM that kills a stage with no other symptom, which is why it is classified rather than reported.
_SIGKILL_EXIT_CODE = 137

#: Ray's own job states, mapped onto the port's vocabulary. Ray's `STOPPED` is a CANCELLED run rather
#: than a failure: the platform prices the two differently — a cancel is somebody's decision, a failure
#: licenses a resubmit — and collapsing them would make a deliberate stop look like an outage.
_RUN_STATE = {
    "PENDING": RunState.PENDING,
    "RUNNING": RunState.RUNNING,
    "SUCCEEDED": RunState.SUCCEEDED,
    "FAILED": RunState.FAILED,
    "STOPPED": RunState.CANCELLED,
}

#: `submit_or_reattach`'s answers, mapped onto the port's. `already_failed` has no `SubmitOutcome` and
#: deliberately so — it is the TRAIN contract's terminal answer, and the stage lane this adapter serves
#: passes `on_terminal_failure="resubmit"`, so it cannot arrive here. Absent rather than guessed: an
#: outcome invented for a branch that cannot happen is a claim nobody can check.
_SUBMIT_OUTCOME = {
    "submitted": SubmitOutcome.SUBMITTED,
    "reattached": SubmitOutcome.REATTACHED,
    "resubmitted": SubmitOutcome.RESUBMITTED,
}


class RayJobsApiExecutor:
    """Submits a stage to a STANDING Ray cluster through the dashboard Jobs API.

    The handle is the submission id the caller already derived, never re-derived here — `RunHandle`'s
    own docstring states why, and `ray_submit.stage_submission_id` was extracted for the same reason.
    """

    name = RAY_ENGINE
    #: CANCEL because a job can be deleted by id; FAILURE_DETAIL because `job_failure` classifies a real
    #: reason rather than handing back a log line. DURABLE_RECORD deliberately absent — see the module
    #: docstring; its absence is load-bearing for the resubmit machinery.
    capabilities = frozenset({Capability.CANCEL, Capability.FAILURE_DETAIL})

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        #: Injected for tests; production passes the lane's pooled client. Held rather than created per
        #: call because the Jobs API is one host and a client per submission would leak connections.
        self._client = client

    async def _http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        from medallion.services.ray_submit import ray_client

        return await ray_client()

    def validate_task(self, registration: TaskRegistration) -> None:
        """Refuse a declaration belonging to another engine, with the reason the door's 422 will carry."""
        if registration.engine != RAY_ENGINE:
            raise WrongEngineError(f"task {registration.task!r} is registered for engine {registration.engine!r}, and this executor runs {RAY_ENGINE!r}")

    def job_metadata(self, order: WorkOrder) -> dict[str, str]:
        """Ray job metadata, DERIVED from the order ([[LH-159]]).

        These five facts are read BACK off the Jobs API, which is why they are stamped at all:
        `rask.originator` is how a job that died is still attributed to the person it was for, and
        `rask.transform` names the declaration an operator wrote. A submission without them is not
        merely thinner — `.claude/skills/rask-notifications` prices it exactly: a run that names nobody
        is undeliverable rather than under-delivered, and fails silently.

        DERIVED RATHER THAN HANDED IN. Every fact lives on the order, so this needs no widening of
        `submit()`: metadata passed per call would let each adapter render the same facts its own way,
        which is the divergence `to_env()` being "the ONE serialization" exists to prevent.

        EMPTY IS OMITTED, matching the lane and [[XC-066]]'s rule for `runtime_env`: a key carrying
        `""` reads as a stamped fact that is simply absent, and absence is the honest claim.
        """
        facts = (
            ("rask.originator", order.identity.originator),
            ("rask.project", order.identity.project),
            ("rask.token", order.stamp.token),
            ("rask.stage", order.stamp.stage),
            ("rask.transform", order.stamp.transform),
        )
        return {key: value for key, value in facts if value}

    async def submit(self, order: WorkOrder, registration: TaskRegistration) -> tuple[RunHandle, SubmitOutcome]:
        """Start the job, or re-attach to the one already doing this work.

        The body is the order's own `to_env()` — the ONE serialization, as `WorkOrder` states — so no
        adapter hand-rolls the wire shape. `on_terminal_failure="resubmit"` is the STAGE contract: a
        redelivery must retry on a healthy worker rather than re-observe the same failure until the
        trigger is silently dropped.
        """
        client = await self._http()
        body: dict[str, Any] = {
            "submission_id": order.idempotency_key,
            "entrypoint": registration.command,
            "runtime_env": {"env_vars": order.to_env()},
            # STAMPED FROM THE ORDER ([[LH-159]]) — see `job_metadata`. Without it a submission through
            # the port would lose the facts that are read BACK off the Jobs API, and losing
            # `rask.originator` means a job that dies can no longer be attributed to the person it was
            # for: undeliverable rather than under-delivered.
            "metadata": self.job_metadata(order),
        }
        answered = await ray_jobs_api.submit_or_reattach(client, order.idempotency_key, body, on_terminal_failure="resubmit")
        return RunHandle(engine=RAY_ENGINE, handle=order.idempotency_key), _SUBMIT_OUTCOME[answered]

    async def status(self, handle: RunHandle) -> RunState:
        """Where the run is. An id the cluster has no record of is UNKNOWN, never FAILED.

        The distinction is the resubmit decision: `job_status` answers ``None`` for an id the head has
        never seen OR has forgotten across a restart, and reporting that as FAILED would turn a lost
        record into a fabricated outcome.
        """
        raw = await ray_jobs_api.job_status(await self._http(), handle.handle)
        return _RUN_STATE.get(raw or "", RunState.UNKNOWN)

    async def failure(self, handle: RunHandle) -> RunFailure | None:
        """Why it failed, in the port's vocabulary — or ``None`` when it did not fail.

        TRANSLATED, not forwarded. `RayJobFailure` is Ray's own post-mortem shape and handing it back
        would put an engine's type through a port whose whole purpose is that the platform does not
        know which engine ran the work.

        The classification is the part worth getting right: **exit code 137 is SIGKILL under any
        engine** — the host-RAM OOM that kills a stage with no other symptom — which is why `RunFailure`
        carries `exit_code` as a portable fact and why `oom` is worth separating from a driver error.
        Ray's own `error_type` is preferred when it says something; `unknown` is answered rather than
        guessed, because a wrong classification is worse than an admitted absence for a platform that
        branches on it.
        """
        raw = await ray_jobs_api.job_failure(await self._http(), handle.handle)
        if raw is None:
            return None
        kind = "oom" if raw.driver_exit_code == _SIGKILL_EXIT_CODE else (raw.error_type or "unknown")
        return RunFailure(kind=kind, message=raw.message or "", exit_code=raw.driver_exit_code)

    async def cancel(self, handle: RunHandle) -> None:
        """Stop the job by deleting it, which is what `CANCEL` promises."""
        await (await self._http()).delete(f"/api/jobs/{handle.handle}")

    async def result(self, handle: RunHandle) -> Any:  # noqa: ANN401 — the port's own return shape; see executor.py
        """Refused, because this engine does not advertise `Capability.RESULT`.

        The job writes the destination in ANOTHER process on another node; nothing here ever holds the
        table, so there is no measurement to hand back and never was. The caller reads the capability
        and measures the destination instead — which is what this lane has always done
        (`measure_stage` reconstructs the column edges from the on-disk schemas precisely because this
        process never saw the write).

        Raising rather than returning `None` keeps the two answers distinguishable: `None` would read
        as "the run produced nothing", and a caller cannot branch on a value carrying two meanings.
        """
        raise NotImplementedError(f"{RAY_ENGINE} writes out-of-process and advertises no RESULT capability; measure the destination for run {handle.handle}")
