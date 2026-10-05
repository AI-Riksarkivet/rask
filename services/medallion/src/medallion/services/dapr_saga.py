"""The Dapr Workflow adapter for the `SagaClient` port — the one place the engine is named.

`service-kit`'s `saga.SagaClient` says what the platform may ask of a workflow engine; this says how
Dapr answers. It is the workflow-plane twin of `inprocess_executor` / `rayjob_executor` one layer
down, and it exists so that a service can start a saga without importing `dapr.ext.workflow` inside a
function body to do it. No Ray lane starts one: a stage run and a training run are each a plan (CP-029),
and the promotion review is the workflow left to move onto this port (LH-226).

THE IMPORT IS LAZY: `dapr.ext.workflow` pulls the workflow runtime, and a module that starts a saga is
also imported by paths that never run one (the test suite, a compute-off dev stack). An adapter that
imported it at module scope would put the engine back into the import graph the port exists to keep it
out of.
"""

from __future__ import annotations

import logging
from typing import Any

from service_kit.lakehouse.saga import SagaHandle, SagaStart


log = logging.getLogger(__name__)


class DaprSagaClient:
    """`SagaClient` over Dapr Workflow.

    Satisfies the protocol structurally — no inheritance, the same shape `Executor`'s adapters use, so
    the port stays a description of behaviour rather than a base class services must derive from.
    """

    def start(self, *, saga: Any, payload: dict[str, Any], instance_id: str) -> SagaHandle:  # noqa: ANN401 — `saga` is Dapr's own workflow callable; the port deliberately does not describe it
        """Schedule `saga`, treating an existing instance as success.

        A SCHEDULE FAILURE IS TWO EVENTS WEARING ONE EXCEPTION and they need opposite answers. "This
        instance already exists" means a watcher is already on this exact job and the trigger is fully
        handled. Anything else — no sidecar, an actor state store the app-id is not scoped to, the
        engine down — means NOTHING is watching, and swallowing it would ack a trigger whose work
        never starts: the job is submitted BY the saga, so no saga means no job at all.

        So existence is CHECKED rather than assumed. An unscoped state store is the likeliest form of
        the second case (`values.yaml` scopes `medallion` for exactly this, and daprd cannot hot-reload
        an actor state store), and it is precisely the one a blanket swallow renders as a silent
        success on every delivery.
        """
        client = self._client()
        try:
            client.schedule_new_workflow(workflow=saga, input=payload, instance_id=instance_id)
        except Exception:
            if not self._exists(client, instance_id):
                raise
            log.info("saga_reattach", extra={"instance_id": instance_id})
            return SagaHandle(instance_id=instance_id, outcome=SagaStart.ALREADY_RUNNING)
        return SagaHandle(instance_id=instance_id, outcome=SagaStart.STARTED)

    def exists(self, instance_id: str) -> bool:
        return self._exists(self._client(), instance_id)

    @staticmethod
    def _client() -> Any:  # noqa: ANN401 — the engine's own client type, which the port does not name
        import dapr.ext.workflow as wf

        return wf.DaprWorkflowClient()

    @staticmethod
    def _exists(client: Any, instance_id: str) -> bool:  # noqa: ANN401
        """Whether Dapr holds a workflow under this id.

        `get_workflow_state` raises for an unknown instance on some sidecar versions and returns None
        on others, so BOTH are read as absent — an id nobody is watching. A transport failure is
        indistinguishable from absence here and is deliberately read as absence: the caller's next
        move is to re-raise the original schedule error, which is the safe direction.
        """
        try:
            return client.get_workflow_state(instance_id) is not None
        except Exception:  # noqa: BLE001 — unknown instance and unreachable engine both mean "not watching"
            return False
