"""The IN-PROCESS engine, as the platform's `Executor` port sees it — §7.4 step 2.

docs/DECISIONS.md "The compute plane is decoupled" calls this "the cheapest possible proof the contract is real, because
the second engine already exists". A port with one implementation is a description; this is what
makes it a contract.

**A SYNCHRONOUS ENGINE IS A LEGITIMATE ONE, and proving that is half the value.** `submit` runs the
work to completion and returns a handle already in a terminal state; `status` answers from the
recorded outcome; `cancel` refuses, because this engine does not advertise `Capability.CANCEL`. If
the port could not express that, it would be a Ray-shaped interface wearing a neutral name — which is
exactly the coupling §7.4 exists to remove.

**IT RETURNS A HANDLE, NEVER THE DATA.** The caller measures the written dataset afterwards, the same
way the Ray branch always has, and that is the contract rather than a concession: §2.5's whole rule is
that the platform RE-DERIVES what was produced instead of believing a self-report. The equivalence is
exact rather than approximate — `transform_stage` builds its own `WriteResult` from
``measure(to_uri) + previous_rows + _column_map(upstream.schema, written, blob_field_names(upstream))``,
and `measure_stage` computes the identical three, from the identical expressions.

**Capabilities are what the resubmit machinery reads.** No `DURABLE_RECORD`: this engine's record dies
with the process, and `executor.may_resubmit` therefore permits a resubmit on `UNKNOWN` — correct
here, because a lost record means the work was lost with it. Against Ray the same absence is a
measured fact (its GCS is not fault-tolerant in this estate); against an engine that DID promise
durability the machinery would be a spurious double-submit, which is the whole reason the rule is a
property of the port rather than a habit of each caller.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi.concurrency import run_in_threadpool

from medallion.services.compute import transform_stage
from service_kit.lakehouse.executor import Capability, RunFailure, RunHandle, RunState, SubmitOutcome
from service_kit.lakehouse.task_registry import TaskRegistration
from service_kit.lakehouse.work_order import WorkOrder


log = logging.getLogger(__name__)

#: The engine name this adapter answers to in `task_registry`. It matches `engine_choice`'s constant
#: because they are the same string doing the same job — a task registered for `inprocess` is a task
#: this executor runs, and the chooser and the runner disagreeing about that spelling would route work
#: to an engine that then refuses it.
IN_PROCESS_ENGINE = "inprocess"


class WrongEngineError(ValueError):
    """The task belongs to another executor.

    Refused rather than run: a declaration meant for a different plane must not be executed here on
    the grounds that this engine happens to be available, which is how the wrong program rewrites a
    tenant's data while every status says success.
    """


class InProcessExecutor:
    """Runs a stage transform in THIS process. Conforms to `service_kit.lakehouse.executor.Executor`.

    The run record is per-INSTANCE and dies with the process, which is the honest shape for a
    synchronous engine and is exactly what the absent `DURABLE_RECORD` capability advertises. A caller
    holding a handle across a restart gets `UNKNOWN`, and `may_resubmit` then permits a resubmit —
    correct, because there is nothing to reattach to.
    """

    name = IN_PROCESS_ENGINE
    #: `FAILURE_DETAIL` only. No `CANCEL` — the work is over before `submit` returns, so there is
    #: nothing to stop; no `DURABLE_RECORD` — see the class docstring.
    capabilities = frozenset({Capability.FAILURE_DETAIL})

    def __init__(self, storage_options: Callable[[], dict[str, str]]) -> None:
        #: A CALLABLE, not a dict: credentials are resolved per run, and a value captured at
        #: construction would be as old as the process. `WorkOrder.credential_ref` names a credential
        #: and deliberately never carries one, so resolving it is the executor's job.
        self._storage_options = storage_options
        self._state: dict[str, RunState] = {}
        self._failures: dict[str, RunFailure] = {}
        self._results: dict[str, Any] = {}

    def validate_task(self, registration: TaskRegistration) -> None:
        """Refuse at DECLARATION time a task this engine cannot honour.

        Raises rather than returning a bool so the refusal carries its reason to the door's 422: an
        operator told "no" without being told which claim this engine cannot meet has to guess, and
        guessing at a declaration door is what the registry exists to end.
        """
        if registration.engine != IN_PROCESS_ENGINE:
            raise WrongEngineError(f"task {registration.task!r} is registered for engine {registration.engine!r}, and this executor runs {IN_PROCESS_ENGINE!r}")

    async def submit(self, order: WorkOrder, registration: TaskRegistration) -> tuple[RunHandle, SubmitOutcome]:
        """Run the transform and return a handle already in a terminal state.

        The order's `idempotency_key` IS the handle, which makes a redelivered order re-attach to the
        recorded outcome rather than running the work twice — the same property a deterministic Ray
        submission id gives, expressed with the only durable identity a synchronous engine has.
        """
        self.validate_task(registration)
        handle = RunHandle(engine=IN_PROCESS_ENGINE, handle=order.idempotency_key)
        if (recorded := self._state.get(handle.handle)) is not None and recorded in {RunState.SUCCEEDED, RunState.FAILED}:
            return handle, SubmitOutcome.REATTACHED
        try:
            self._results[handle.handle] = await run_in_threadpool(
                transform_stage,
                order.source.uri,
                order.destination.uri,
                self._storage_options(),
                stage=order.stamp.stage,
                lineage=_lineage_of(order),
                dataset_id=order.destination.table_id or None,
            )
        except Exception as exc:  # noqa: BLE001 — classified into the port's own vocabulary below
            self._state[handle.handle] = RunState.FAILED
            self._failures[handle.handle] = _classify(exc)
            log.warning("inprocess_run_failed", extra={"handle": handle.handle, "error_type": type(exc).__name__, "error": str(exc)})
            return handle, SubmitOutcome.SUBMITTED
        self._state[handle.handle] = RunState.SUCCEEDED
        return handle, SubmitOutcome.SUBMITTED

    async def status(self, handle: RunHandle) -> RunState:
        """The recorded outcome, or `UNKNOWN` when this process has no record of the handle.

        `UNKNOWN` rather than a guess, which is the substantive part of the port: today's `job_status`
        answers `None` for a 404 and every caller re-derives three distinct meanings from it by hand.
        A caller that gets it wrong either resubmits live work or waits on a job that is gone.
        """
        return self._state.get(handle.handle, RunState.UNKNOWN)

    async def failure(self, handle: RunHandle) -> RunFailure | None:
        return self._failures.get(handle.handle)

    async def cancel(self, handle: RunHandle) -> None:
        """Refused, because this engine does not advertise `Capability.CANCEL`.

        The work is over before `submit` returns, so there is nothing to stop. Raising is the honest
        answer; a silent no-op would let a caller believe it had stopped something.
        """
        raise NotImplementedError(f"{IN_PROCESS_ENGINE} runs synchronously and advertises no CANCEL capability; run {handle.handle} is already over")

    def result(self, handle: RunHandle) -> Any:  # noqa: ANN401 — a WriteResult; typing it here would pull the medallion's model into the port's shape
        """This run's measured output, for a caller that wants it without a second read.

        BEYOND the port, and only ever an optimisation: the platform's contract is to re-derive what
        was written from the dataset (§2.5), and a caller that ignores this is not weaker for it.
        """
        return self._results.get(handle.handle)


def _lineage_of(order: WorkOrder) -> Any:  # noqa: ANN401 — a LineageDoc, imported lazily to keep this module's import cheap
    """The order's consume-layer provenance document, parsed back into the writer's model.

    Carried as a STRING on the order because a work order crosses process boundaries and a model does
    not. Absent means the run supplies no document, which `stamp_stage` reads as "drop any inherited
    one" rather than "keep the parent's" — the parent's document describes the parent's run.
    """
    if not order.stamp.lineage_document:
        return None
    from lineage_kit.consume import LineageDoc

    return LineageDoc.model_validate(json.loads(order.stamp.lineage_document))


def _classify(exc: Exception) -> RunFailure:
    """The engine's own error, in terms the platform can branch on.

    `kind` is a CLASSIFICATION, not a log line: a caller deciding whether to retry cannot branch on
    free text. `oom` is separated from `driver_error` because they need opposite operator responses —
    one is a sizing problem, the other a code one.
    """
    text = str(exc)
    kind = "oom" if isinstance(exc, MemoryError) or "out of memory" in text.lower() else "driver_error"
    # THE ENGINE'S OWN MESSAGE, VERBATIM. It reaches an operator through the run's FAIL lineage event,
    # which has one `errorMessage` field and no room for a classification — so prefixing the type here
    # would push the diagnosis behind a label a machine reads and a person does not need. `kind` is
    # where the classification lives, and the exception type rides the log line beside it.
    return RunFailure(kind=kind, message=text[:2000])
