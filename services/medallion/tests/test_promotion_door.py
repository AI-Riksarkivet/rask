"""The approval door, and WHY it lives on the producer rather than the stage runner that held the promotion.

`raise_workflow_event` resolves the workflow actor through the app-id of the process that CALLS it.
The quality gate runs in the `silver-to-gold` stage runner, so the obvious design hosts `promotion_review`
there — and then the approve route has to be there too, on a bus-only worker with no gateway row and
no Ingress path. Putting only the ROUTE on `medallion-producer` (which has both, and already runs the
dual-auth door for `/produce` and `/train`) does not work either: the producer's sidecar looks for the
instance under its own app-id, does not find it, and **accepts the call anyway**. Not an error — a
success, for an approval that will never be delivered, with the promotion left to expire on its timer.

So the producer hosts the workflow AND the door, and the stage runner reaches it over the bus like every
other cascade hop. Stage runners stay bus-only, which is what they are.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from dapr.ext.workflow.workflow_state import WorkflowStatus
from lance_namespace import PermissionDeniedError, TableNotFoundError

from medallion.api.promotions import decide_promotion, handle_promotion_held, instance_for
from medallion.services.dapr_saga import DaprSagaClient


class _State:
    def __init__(self, payload: dict[str, Any], status: WorkflowStatus) -> None:
        self.serialized_input = json.dumps(payload)
        self.runtime_status = status


class _WorkflowClient:
    """A double shaped like `DaprWorkflowClient` — including the part that makes this design necessary.

    `raise_workflow_event` records unconditionally, because the real one ACCEPTS an event for an
    instance it does not host. A door that calls it without checking first cannot be caught by
    asserting on the client; it is caught by asserting the client was never reached.
    """

    def __init__(self, *, instances: dict[str, _State] | None = None) -> None:
        self.raised: list[tuple[str, str, Any]] = []
        self.scheduled: list[tuple[str, Any]] = []
        self._instances = dict(instances or {})

    def raise_workflow_event(self, instance_id: str, event_name: str, *, data: Any = None) -> None:
        self.raised.append((instance_id, event_name, data))

    def schedule_new_workflow(self, *, workflow: Any, input: Any, instance_id: str) -> str:  # noqa: A002
        if instance_id in self._instances:
            raise RuntimeError(f"instance {instance_id} already exists")
        self.scheduled.append((instance_id, input))
        self._instances[instance_id] = _State(input, WorkflowStatus.RUNNING)
        return instance_id

    def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> _State | None:
        return self._instances.get(instance_id)


def _held(**over: Any) -> dict[str, Any]:
    """A chart lane's hold for tenant `acme`, named as the stage runner resolved it."""
    base: dict[str, Any] = {
        "token": "tok-1",
        "project": "acme",
        "from_namespace": "acme-silver",
        "from_dataset": "acme-silver$features",
        "to_namespace": "acme-gold",
        "to_dataset": "acme-gold$catalog",
        "operation": "aggregate_gold",
        "author": "analyst",
        "version": 7,
        "reasons": ["row_delta_band"],
        "approver": "CiQwOGE4Njg0Yi1kYjg4",
        "originator": "CiQwOGE4Njg0Yi1kYjg4",
        "approval_hours": 72,
    }
    return base | over


class TestTheHoldReachesTheProducerOverTheBus:
    @pytest.mark.asyncio
    async def test_a_redelivered_hold_REATTACHES_instead_of_asking_twice(self) -> None:
        """Dapr redelivers, so a handler may run twice. What must not happen is two reviews of one
        promotion, each asking the approver separately."""
        client = _WorkflowClient()

        first = await handle_promotion_held({"data": _held()}, client=DaprSagaClient(client))
        second = await handle_promotion_held({"data": _held()}, client=DaprSagaClient(client))

        assert first == second == {"status": "SUCCESS"}
        assert len(client.scheduled) == 1

    @pytest.mark.asyncio
    async def test_an_engine_outage_RETRIES(self) -> None:
        """Distinct from the malformed case: the event is fine and nothing is watching the promotion.
        Acking that would lose the review entirely."""

        class _Down(_WorkflowClient):
            def schedule_new_workflow(self, **kwargs: Any) -> str:
                raise RuntimeError("connection refused")

            def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> None:
                raise RuntimeError("connection refused")

        result = await handle_promotion_held({"data": _held()}, client=DaprSagaClient(_Down()))

        assert result == {"status": "RETRY"}


class TestTheDecisionDoor:
    @pytest.mark.asyncio
    async def test_an_approval_raises_the_event_INTO_the_hosting_app(self) -> None:
        client = _WorkflowClient(instances={instance_for("tok-1"): _State(_held(), WorkflowStatus.RUNNING)})

        result = await decide_promotion(instance_for("tok-1"), approved=True, subject="CiQwOGE4Njg0Yi1kYjg4", client=DaprSagaClient(client))

        assert result["status"] == "accepted"
        assert result["approved"] is True
        assert client.raised == [(instance_for("tok-1"), "promotion_decision", {"approved": True, "subject": "CiQwOGE4Njg0Yi1kYjg4"})]

    @pytest.mark.asyncio
    async def test_an_UNKNOWN_instance_is_404_and_never_a_silent_ACCEPT(self) -> None:
        """The failure this whole design exists to avoid. The client accepts the raise regardless, so
        the door must check FIRST — an operator who is told their approval landed, on a promotion that
        then expires, has been lied to by a success."""
        client = _WorkflowClient()

        with pytest.raises(TableNotFoundError):
            await decide_promotion(instance_for("nope"), approved=True, subject="CiQwOGE4Njg0Yi1kYjg4", client=DaprSagaClient(client))

        assert client.raised == [], "the door must not reach the client for an instance it does not host"

    @pytest.mark.asyncio
    async def test_a_TERMINAL_instance_is_refused_with_its_status(self) -> None:
        """The same lie in its commonest form: approving a promotion that already expired. The engine
        accepts the event and discards it, because the instance has completed."""
        client = _WorkflowClient(instances={instance_for("tok-1"): _State(_held(), WorkflowStatus.COMPLETED)})

        with pytest.raises(TableNotFoundError, match="COMPLETED"):
            await decide_promotion(instance_for("tok-1"), approved=True, subject="CiQwOGE4Njg0Yi1kYjg4", client=DaprSagaClient(client))

        assert client.raised == []

    @pytest.mark.asyncio
    async def test_a_decision_naming_NOBODY_is_refused(self) -> None:
        """The subject is recorded into lineage as `promotion_decided_by`. The service-token path of
        the shared auth door returns no subject at all — an unattributable approval is not an approval,
        and the workflow would BLOCK on it anyway, three hops later and unexplained."""
        client = _WorkflowClient(instances={instance_for("tok-1"): _State(_held(), WorkflowStatus.RUNNING)})

        with pytest.raises(PermissionDeniedError):
            await decide_promotion(instance_for("tok-1"), approved=True, subject="", client=DaprSagaClient(client))

        assert client.raised == []


class TestTheDoorAuthorizesAgainstTHISPromotion:
    @pytest.mark.asyncio
    async def test_the_gate_runs_against_the_promotions_OWN_destination(self) -> None:
        """The route carries an instance id and nothing else — no project, no namespace. Reading them
        off a query param would let the caller choose the object their own permission is checked
        against, so they are read from the durable instance instead."""
        client = _WorkflowClient(instances={instance_for("tok-1"): _State(_held(), WorkflowStatus.RUNNING)})
        gated: list[tuple[str, str]] = []

        async def _authorize(*, subject: str, obj: str) -> None:
            gated.append((subject, obj))

        await decide_promotion(instance_for("tok-1"), approved=True, subject="alice", client=DaprSagaClient(client), authorize=_authorize)

        assert gated == [("alice", "namespace:acme-gold")], "can_promote is a rung on the DESTINATION stage"

    @pytest.mark.asyncio
    async def test_a_denied_caller_never_reaches_the_workflow(self) -> None:
        client = _WorkflowClient(instances={instance_for("tok-1"): _State(_held(), WorkflowStatus.RUNNING)})

        async def _deny(*, subject: str, obj: str) -> None:
            raise PermissionDeniedError(f"{subject} lacks can_promote on {obj}")

        with pytest.raises(PermissionDeniedError):
            await decide_promotion(instance_for("tok-1"), approved=True, subject="mallory", client=DaprSagaClient(client), authorize=_deny)

        assert client.raised == []

    @pytest.mark.asyncio
    async def test_a_DECLARED_lane_gates_on_the_namespace_the_stage_resolved(self) -> None:
        """A lane declared through the catalog door may name tenant-free ids, and the stage runner checks
        its own `can_promote` on exactly the namespace it resolved (`namespace:curated`). The approver
        must be gated on the same object: re-qualifying it names `namespace:acme-curated`, which no
        tuple mentions, so every validator would be refused."""
        declared = _held(from_namespace="landing", from_dataset="landing$events", to_namespace="curated", to_dataset="curated$catalog")
        client = _WorkflowClient(instances={instance_for("tok-1"): _State(declared, WorkflowStatus.RUNNING)})
        gated: list[tuple[str, str]] = []

        async def _authorize(*, subject: str, obj: str) -> None:
            gated.append((subject, obj))

        await decide_promotion(instance_for("tok-1"), approved=True, subject="alice", client=DaprSagaClient(client), authorize=_authorize)

        assert gated == [("alice", "namespace:curated")]
