"""The Dapr Workflow adapter for the `SagaClient` port — the one place the engine's client is named.

`service-kit`'s `saga.SagaClient` says what the platform may ask of a workflow engine; this says how
Dapr answers. It is the workflow-plane twin of `inprocess_executor` / `rayjobs_api_executor` one layer
down. Its caller is the promotion review's door (`medallion.api.promotions`), which starts, reads and
answers `promotion_review` through it; no Ray lane starts a saga, since a stage run and a training run
are each a plan (CP-029).

THE IMPORT IS LAZY: `dapr.ext.workflow` pulls the workflow runtime, and a module that starts a saga is
also imported by paths that never run one (the test suite, a compute-off dev stack, the cascade head
with quality review off). An adapter that imported it at module scope would put the engine back into
the import graph the port exists to keep it out of.
"""

from __future__ import annotations

import logging
from typing import Any

from service_kit.lakehouse.saga import SagaHandle, SagaStart, SagaState, SagaStatus


log = logging.getLogger(__name__)


class DaprSagaClient:
    """`SagaClient` over Dapr Workflow.

    Satisfies the protocol structurally — no inheritance, the same shape `Executor`'s adapters use, so
    the port stays a description of behaviour rather than a base class services must derive from.

    ONE ENGINE CLIENT PER ADAPTER: `DaprWorkflowClient` opens a gRPC channel to the sidecar, so the app
    builds one adapter in its lifespan and the client is built on first use and kept. `client` is that
    engine client when the caller already holds one.
    """

    def __init__(self, client: Any = None) -> None:  # noqa: ANN401 — the engine's own client type, which the port does not name
        self._engine_client = client

    def start(self, *, saga: Any, payload: dict[str, Any], instance_id: str) -> SagaHandle:  # noqa: ANN401 — `saga` is Dapr's own workflow callable; the port deliberately does not describe it
        """Schedule `saga`, treating an existing instance as success.

        A SCHEDULE FAILURE IS TWO EVENTS WEARING ONE EXCEPTION and they need opposite answers. "This
        instance already exists" means a watcher is already on this exact job and the trigger is fully
        handled. Anything else — no sidecar, an actor state store the app-id is not scoped to, the
        engine down — means NOTHING is watching, and swallowing it would ack a trigger whose work
        never starts.

        So existence is CHECKED rather than assumed. An unscoped state store is the likeliest form of
        the second case (`values.yaml` scopes `medallion` for exactly this, and daprd cannot hot-reload
        an actor state store), and it is precisely the one a blanket swallow renders as a silent
        success on every delivery.
        """
        try:
            self._engine().schedule_new_workflow(workflow=saga, input=payload, instance_id=instance_id)
        except Exception:
            if not self._held(instance_id):
                raise
            log.info("saga_reattach", extra={"instance_id": instance_id})
            return SagaHandle(instance_id=instance_id, outcome=SagaStart.ALREADY_RUNNING)
        return SagaHandle(instance_id=instance_id, outcome=SagaStart.STARTED)

    def state(self, instance_id: str) -> SagaState | None:
        """Read the instance with its stored input, mapping Dapr's status onto the port's.

        The mapping is by enum IDENTITY, not by member name: the names are the library's to change, the
        members are what it compares. A status this table does not name is `UNKNOWN`, which is not live.
        """
        from dapr.ext.workflow.workflow_state import WorkflowStatus

        statuses = {
            WorkflowStatus.PENDING: SagaStatus.PENDING,
            WorkflowStatus.RUNNING: SagaStatus.RUNNING,
            WorkflowStatus.SUSPENDED: SagaStatus.SUSPENDED,
            WorkflowStatus.COMPLETED: SagaStatus.COMPLETED,
            WorkflowStatus.FAILED: SagaStatus.FAILED,
            WorkflowStatus.TERMINATED: SagaStatus.TERMINATED,
            WorkflowStatus.STALLED: SagaStatus.STALLED,
        }
        held = self._engine().get_workflow_state(instance_id, fetch_payloads=True)
        if held is None:
            return None
        return SagaState(
            instance_id=instance_id,
            status=statuses.get(getattr(held, "runtime_status", None), SagaStatus.UNKNOWN),
            input=held.serialized_input,
        )

    def signal(self, instance_id: str, event: str, data: Any) -> None:  # noqa: ANN401 — the event body is the saga's own contract with its caller
        self._engine().raise_workflow_event(instance_id, event, data=data)

    def _engine(self) -> Any:  # noqa: ANN401 — the engine's own client type, which the port does not name
        if self._engine_client is None:
            import dapr.ext.workflow as wf

            self._engine_client = wf.DaprWorkflowClient()
        return self._engine_client

    def _held(self, instance_id: str) -> bool:
        """Whether Dapr holds a workflow under this id, with an unanswerable read taken as 'no'.

        `get_workflow_state` raises for an unknown instance on some sidecar versions and returns None
        on others, so BOTH are read as absent — an id nobody is watching. A transport failure is
        indistinguishable from absence here and is deliberately read as absence: the caller's next
        move is to re-raise the original schedule error, which is the safe direction.
        """
        try:
            return self.state(instance_id) is not None
        except Exception:  # noqa: BLE001 — unknown instance and unreachable engine both mean "not watching"
            return False
