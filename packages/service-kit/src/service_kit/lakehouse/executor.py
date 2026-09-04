"""The `Executor` port — what the platform may ask of a compute engine, naming none of them.

`open_compute-decoupling.md` §2.4, step 1 of the owner-ordered §7.4. Together with `work_order.py` this
is the whole of the decoupling claim: `WorkOrder` says WHAT must happen, this says how the platform
asks for it and how it learns the outcome. **`service-kit` must not gain a `ray` dependency**, and this
module adds none — it imports pydantic and the standard library.

`UNKNOWN` REPLACES AN OVERLOADED `None`, which is the substantive change rather than a rename. Today's
`job_status` returns `None` for a 404 and the medallion workflow disentangles THREE distinct meanings
from it by hand — not-yet-registered, record-lost, and a transport blip — with `seen`, `vanished`,
`never_registered` and a poll ceiling. A port answering `None` forces every caller to re-derive that,
and a caller that gets it wrong either resubmits live work or waits 24 hours on a job that is gone.

THE RESUBMIT MACHINERY IS CAPABILITY-GATED, NOT UNCONDITIONAL. The medallion's `MAX_UNSEEN_POLLS` and
`MAX_RESUBMITS` are justified by an engine-specific durability defect it names outright: Ray's GCS is
not fault-tolerant in this estate (no external Redis, a standing rule), so a head restart takes every
job record with it. Against an engine that advertises `DURABLE_RECORD` the same machinery is a spurious
DOUBLE-SUBMIT — a second copy of work nothing lost. `may_resubmit` makes that a property of the port
rather than a rule each caller must remember.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from service_kit.lakehouse.task_registry import TaskRegistration
from service_kit.lakehouse.work_order import WorkOrder


class RunState(StrEnum):
    """Where a submitted run is. `UNKNOWN` means the engine has no record of this handle."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


#: The states a run cannot leave. `UNKNOWN` is deliberately NOT here: the engine may not know YET, and
#: treating ignorance as terminal is how a running job is declared finished.
TERMINAL: Final = frozenset({RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED})


class Capability(StrEnum):
    """What an engine promises. Absence is the default, so a new adapter is assumed to promise nothing."""

    #: A submitted run survives a control-plane restart. Its absence is what licenses a resubmit.
    DURABLE_RECORD = "durable_record"
    CANCEL = "cancel"
    FAILURE_DETAIL = "failure_detail"


class RunFailure(BaseModel):
    """Why a run failed, classified by the adapter into terms the platform can act on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Adapter-classified: "driver_error" | "oom" | "infra" | "unknown". A free-form log line is not a
    #: classification, and the platform cannot branch on one.
    kind: str
    message: str
    #: POSIX. 137 is SIGKILL under any engine — a portable fact, unlike a message string.
    exit_code: int | None = None


class SubmitOutcome(StrEnum):
    """What a submit actually did, so a caller can price it."""

    SUBMITTED = "submitted"
    REATTACHED = "reattached"
    #: A terminally-FAILED prior run was replaced. Never a live one.
    RESUBMITTED = "resubmitted"


class RunHandle(BaseModel):
    """What `submit` returned, carried to every later call.

    NEVER RE-DERIVED by the watcher. `ray_submit.py` records the measured defect: the submitter and the
    watcher must name the same job, and a second inline derivation is how a poller ends up watching an
    id the submitter never used, reporting a healthy job as missing forever.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine: str
    handle: str


def may_resubmit(state: RunState, *, capabilities: frozenset[Capability]) -> bool:
    """May a caller submit this work again?

    Only for `UNKNOWN`, and only from an engine that does NOT promise a durable record. Every other
    state is knowledge: resubmitting a RUNNING job puts two writers on one destination, and
    resubmitting a SUCCEEDED one repeats work that already landed.
    """
    return state is RunState.UNKNOWN and Capability.DURABLE_RECORD not in capabilities


@runtime_checkable
class Executor(Protocol):
    """One compute engine, as the platform sees it.

    `runtime_checkable` so conformance can be ASKED rather than assumed — the same reason
    `ingest/catalog.py` states for its capability Protocols: `isinstance` asks exactly the question the
    runtime is asking, and a partial implementation is refused at the seam instead of raising later.
    """

    name: str
    capabilities: frozenset[Capability]

    def validate_task(self, registration: TaskRegistration) -> None:
        """Refuse at DECLARATION time a task this engine cannot honour.

        Raises rather than returning a bool so the refusal carries its reason to the door's 422 — an
        operator told "no" without being told which of the registration's claims this engine cannot
        meet has to guess, and guessing at a declaration door is what the registry exists to end.
        """
        ...

    async def submit(self, order: WorkOrder, registration: TaskRegistration) -> tuple[RunHandle, SubmitOutcome]:
        """Start the work, or re-attach to the run already doing it.

        BOTH ARGUMENTS, and neither is redundant: the order says what must happen (this is the
        platform's, identical across engines), the registration says what running it means here (this
        is the engine's own). Collapsing them would make one of the two a per-engine shape.
        """
        ...

    async def status(self, handle: RunHandle) -> RunState: ...

    async def failure(self, handle: RunHandle) -> RunFailure | None: ...

    async def cancel(self, handle: RunHandle) -> None: ...
