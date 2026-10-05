"""The `SagaClient` port — how a service starts a durable saga, naming no workflow engine.

The mirror of `executor.py` one layer up. The estate has two stacked seams: a WORKFLOW engine
(durable steps, timers, external events) in front of a COMPUTE engine (`Executor`, `WorkOrder`,
`task_registry`), and ingest, batch processing and the quality gate are that pair instantiated three
times rather than three planes. The compute half got a port; this half did not.

WHAT WAS ACTUALLY MISSING, measured 2026-09-06 rather than inferred from an import count. 68
orchestration constructs (`yield ctx.*`, `DaprWorkflowContext`) live in FOUR files, two of which carry
65 — `medallion/workflow.py` and `ingest/workflow.py`. The other ten files that import
`dapr.ext.workflow` carry ZERO: they are registration and client calls. So an engine swap rewrites the
orchestration, which is engine-shaped by nature (Dapr replays a generator, Argo walks a YAML DAG,
Flyte composes decorated tasks — those do not reduce to one interface without becoming the lowest
common denominator of all three). What DOES reduce is a door's reach into the engine to
start, read or answer an instance: the route a person approves a held promotion on should not have to
know which engine is holding it.

THREE OPERATIONS AND NO MORE, because three is what the estate uses: the promotion review is started,
read and answered. `start` is idempotent by contract: a schedule failure is two different events wearing
one exception, and they need opposite answers. "This instance already exists" means a watcher is already
on this exact job and the trigger is fully handled; anything else (no sidecar, an unscoped state store,
the engine down) means NOTHING is watching, and swallowing it would ack a trigger whose work never
starts. `start` returning `ALREADY_RUNNING` makes that distinction the adapter's, not each caller's.
`state` answers what the engine holds under an id, in a status vocabulary this module owns rather than
the engine's enum. `signal` delivers an external event to a waiting saga. There is no `terminate`: no
caller stops a saga, since a stage run and a training run are plans the executor stops (CP-029).

**`service-kit` must not gain a workflow-engine dependency**, and this module adds none: it imports
pydantic and the standard library.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class SagaStart(StrEnum):
    """What starting a saga did — never `None`, which is the overload `executor.py` also refused.

    `ALREADY_RUNNING` is a SUCCESS: the saga is being watched, which is the outcome the caller wanted.
    It is distinguished from `STARTED` because an idempotent re-delivery is worth seeing in a log and
    is not worth an alert.
    """

    STARTED = "started"
    ALREADY_RUNNING = "already_running"


class SagaHandle(BaseModel):
    """The identity of a running saga — what a caller keeps to read or signal it later."""

    model_config = ConfigDict(frozen=True)

    instance_id: str
    outcome: SagaStart


class SagaStatus(StrEnum):
    """Where a saga stands, in the port's words rather than an engine's.

    `PENDING`, `RUNNING` and `SUSPENDED` are LIVE: the saga can still take a signal and act on it.
    `COMPLETED`, `FAILED` and `TERMINATED` are terminal. `STALLED` is an instance the engine holds and
    cannot advance, and `UNKNOWN` is a status the adapter has no word for; neither is live, because an
    engine that accepts a signal for an instance that will never act on it is reporting a success that
    did not happen.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"
    TERMINATED = "terminated"
    STALLED = "stalled"
    UNKNOWN = "unknown"

    @property
    def live(self) -> bool:
        """Whether a saga in this status can still take a signal and act on it."""
        return self in _LIVE


_LIVE = frozenset({SagaStatus.PENDING, SagaStatus.RUNNING, SagaStatus.SUSPENDED})


class SagaState(BaseModel):
    """What the engine holds under one instance id: its status and the input it was started with.

    `input` is the payload as the engine stored it, serialized: a caller that reads a saga back
    validates it into its own model, and a stored input that no longer validates is that caller's
    refusal to make, not the port's.
    """

    model_config = ConfigDict(frozen=True)

    instance_id: str
    status: SagaStatus
    input: str | None = None


@runtime_checkable
class SagaClient(Protocol):
    """Start a durable saga, read where it stands, and deliver it an external event.

    A CALLER-CHOSEN `instance_id` is the whole idempotency story and is required, not optional: the
    estate derives it deterministically from the work (the promotion's run token), so a redelivered
    trigger names the saga that is already handling it. An engine that mints its own id cannot
    provide that, and an adapter for one must derive a deterministic mapping rather than accept a
    generated id — otherwise at-least-once delivery becomes at-least-once EXECUTION.
    """

    def start(self, *, saga: Any, payload: dict[str, Any], instance_id: str) -> SagaHandle:  # noqa: ANN401 — `saga` is the engine's own callable/definition and the port deliberately does not describe it
        """Start `saga` under `instance_id`, or report that it is already running.

        Raises when the saga could NOT be started and is not already running — the case a caller must
        never swallow, because nothing is watching the work.
        """
        ...

    def state(self, instance_id: str) -> SagaState | None:
        """What the engine holds under `instance_id`, or `None` when it holds nothing.

        Raises when the engine cannot answer. Reading an unreachable engine as `None` would tell a
        caller that no saga exists on exactly the path where one may.
        """
        ...

    def signal(self, instance_id: str, event: str, data: Any) -> None:  # noqa: ANN401 — the event body is the saga's own contract with its caller
        """Deliver `event` with `data` to the saga under `instance_id`.

        An engine may accept a signal for an instance it does not host, or one that has finished, and
        discard it. A caller whose answer must arrive reads `state` first and refuses an instance that is
        absent or not `live`.
        """
        ...
