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
common denominator of all three). What DOES reduce is the ACTIVITY layer's reach upward:
`transform.py` and `train.py` each imported `dapr.ext.workflow` inside a function body purely to start
an instance. A service that has just built a `WorkOrder` should not have to know which engine will
carry it.

TWO OPERATIONS AND NO MORE, because two is what the estate uses. `start` is idempotent by contract,
and `exists` is why: a schedule failure is two different events wearing one exception, and they need
opposite answers. "This instance already exists" means a watcher is already on this exact job and the
trigger is fully handled; anything else (no sidecar, an unscoped state store, the engine down) means
NOTHING is watching, and swallowing it would ack a trigger whose work never starts. `start` returning
`ALREADY_RUNNING` makes that distinction the port's, not each caller's — the medallion had to check
the instance by hand to tell them apart.

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
    """The identity of a running saga — what a caller keeps to poll or terminate it later."""

    model_config = ConfigDict(frozen=True)

    instance_id: str
    outcome: SagaStart


@runtime_checkable
class SagaClient(Protocol):
    """Start a durable saga, and answer whether one is already running under an id.

    A CALLER-CHOSEN `instance_id` is the whole idempotency story and is required, not optional: the
    estate derives it deterministically from the work (`stage_submission_id`), so a redelivered
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

    def exists(self, instance_id: str) -> bool:
        """Whether a saga is registered under `instance_id`.

        Separate from `start` because a caller sometimes needs the answer without attempting a start —
        and because an adapter whose engine cannot answer it must say so by raising rather than
        returning `False`, which would read as "safe to start" on the one path where it is not.
        """
        ...
