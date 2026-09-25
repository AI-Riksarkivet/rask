"""The `Executor` port — what the platform may ask of a compute engine, naming none of them.

docs/DECISIONS.md "The compute plane is decoupled", step 1 of the owner-ordered §7.4. Together with `work_order.py` this
is the whole of the decoupling claim: `WorkOrder` says WHAT must happen, this says how the platform
asks for it and how it learns the outcome. **`service-kit` must not gain a `ray` dependency**, and this
module adds none — it imports pydantic and the standard library.

`UNKNOWN` REPLACES AN OVERLOADED `None`, which is the substantive change rather than a rename. Today's
`job_status` returns `None` for a 404 and the medallion workflow disentangles THREE distinct meanings
from it by hand — not-yet-registered, record-lost, and a transport blip — with `seen`, `vanished`,
`never_registered` and a poll ceiling. A port answering `None` forces every caller to re-derive that,
and a caller that gets it wrong either resubmits live work or waits 24 hours on a job that is gone.

`DURABLE_RECORD` IS WHAT A RESUBMIT HAS TO ASK ABOUT. The medallion's `MAX_UNSEEN_POLLS` and
`MAX_RESUBMITS` are justified by an engine-specific durability defect it names outright: Ray's GCS is
not fault-tolerant in this estate (no external Redis, a standing rule), so a head restart takes every
job record with it. Against an engine that advertises `DURABLE_RECORD` the same machinery is a spurious
DOUBLE-SUBMIT — a second copy of work nothing lost — so it is sound only behind an engine that withholds
the capability, which both adapters in this estate do.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

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


class Capability(StrEnum):
    """What an engine promises. Absence is the default, so a new adapter is assumed to promise nothing."""

    #: A submitted run survives a control-plane restart. Its absence is what licenses a resubmit.
    DURABLE_RECORD = "durable_record"
    CANCEL = "cancel"
    FAILURE_DETAIL = "failure_detail"
    #: This engine can hand back what the run measured, so the caller need not read the destination a
    #: second time. It is an OPTIMISATION and never a requirement: §2.5's rule is that the platform
    #: re-derives what was written, so an engine declining this is not weaker, it is ordinary. An engine
    #: that writes out-of-process has nothing to return and must not claim it.
    RESULT = "result"


class RunFailure(BaseModel):
    """Why a run failed, classified by the adapter into terms the platform can act on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Adapter-classified: "driver_error" | "oom" | "infra" | "unknown". A free-form log line is not a
    #: classification, and the platform cannot branch on one.
    kind: str
    message: str
    #: POSIX. 137 is SIGKILL under any engine — a portable fact, unlike a message string.
    exit_code: int | None = None

    def summary(self, message_limit: int) -> str:
        """A one-line cause, with the free text bounded by the CALLER's limit.

        THE LIMIT IS AN ARGUMENT, not a constant, because the ceiling belongs to whatever this string
        is being put INTO — a lineage event published through a claim-check funnel has a different
        budget from a log line — and the port has no business knowing which.

        ORDERED CLASSIFICATION-FIRST so truncation can only ever eat the free-text tail: `kind` and
        `exit_code` are what classify a failure, and a cap that swallowed them would leave the caller
        with a long string saying less than the short one it replaced. Empty when the engine reported
        nothing, so a caller can tell "no cause available" from "the cause is blank".

        It lives on the PORT rather than on an adapter's own post-mortem type because the three facts
        it renders are already engine-free; duplicating the formatting per adapter is how two engines
        end up describing the same failure differently.
        """
        parts: list[str] = []
        if self.kind:
            parts.append(self.kind)
        if self.message:
            flat = " ".join(self.message.split())
            parts.append(flat if len(flat) <= message_limit else f"{flat[:message_limit]}… (truncated)")
        rendered = ": ".join(parts)
        if self.exit_code is not None:
            suffix = f"driver exit {self.exit_code}"
            rendered = f"{rendered} ({suffix})" if rendered else suffix
        return rendered


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


class WrongEngineError(ValueError):
    """The task belongs to another executor.

    Refused rather than run: a declaration meant for a different plane must not be executed here on the
    grounds that this engine happens to be available, which is how the wrong program rewrites a tenant's
    data while every status says success.

    IT LIVES ON THE PORT RATHER THAN IN AN ADAPTER because `validate_task` promises to RAISE, so the
    exception type is part of the contract: two adapters raising two different types would break any
    caller that catches one of them. It moved here when the Ray lane gained its own adapter and the
    second implementation made the shared-ness visible.
    """


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

    async def result(self, handle: RunHandle) -> Any:  # noqa: ANN401 — see below
        """What the run measured, for an engine that advertises `Capability.RESULT`.

        GATED ON THE CAPABILITY, NOT OPTIONAL ON THE PROTOCOL, and the distinction is load-bearing:
        `Executor` is `runtime_checkable`, so a method missing from an adapter makes `isinstance` answer
        False and the adapter unreachable through the registry. Every adapter therefore HAS this
        method, and one that has nothing to return raises — the same shape `cancel` already uses for
        the capability `InProcessExecutor` declines.

        `Any` because the measurement is the platform's own model and `service-kit` must not import a
        service's: the medallion's `WriteResult` carries a `previous_row_count` that lance-ns's
        same-named wire model does not, so neither spelling can stand for the other here. Narrowing it
        would either drop a field the promotion band reads or pull the medallion into the port's shape.

        RAISING RATHER THAN RETURNING `None` is the `UNKNOWN` argument in this docstring's own module:
        `None` would be indistinguishable from a run that measured nothing, and a caller cannot branch
        on a value that means two things. Read the capability, then call this.
        """
        ...
