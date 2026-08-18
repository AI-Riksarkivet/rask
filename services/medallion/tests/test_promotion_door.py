"""The approval door, and WHY it lives on the producer rather than the mover that held the promotion.

`raise_workflow_event` resolves the workflow actor through the app-id of the process that CALLS it.
The quality gate runs in the `silver-to-gold` mover, so the obvious design hosts `promotion_review`
there — and then the approve route has to be there too, on a bus-only worker with no gateway row and
no Ingress path. Putting only the ROUTE on `medallion-producer` (which has both, and already runs the
dual-auth door for `/produce` and `/train`) does not work either: the producer's sidecar looks for the
instance under its own app-id, does not find it, and **accepts the call anyway**. Not an error — a
success, for an approval that will never be delivered, with the promotion left to expire on its timer.

So the producer hosts the workflow AND the door, and the mover reaches it over the bus like every
other cascade hop. Movers stay bus-only, which is what they are.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from dapr.ext.workflow.workflow_state import WorkflowStatus
from lance_namespace import PermissionDeniedError, TableNotFoundError
from medallion.api.promotions import decide_promotion, handle_promotion_held, instance_for


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
    base: dict[str, Any] = {
        "token": "tok-1",
        "project": "acme",
        "from_namespace": "silver",
        "from_dataset": "silver$features",
        "to_namespace": "gold",
        "to_dataset": "gold$catalog",
        "pub_topic": "",
        "reasons": ["row_delta_band"],
        "approver": "CiQwOGE4Njg0Yi1kYjg4",
        "originator": "CiQwOGE4Njg0Yi1kYjg4",
        "approval_hours": 72,
    }
    return base | over


class TestTheHoldReachesTheProducerOverTheBus:
    @pytest.mark.asyncio
    async def test_a_held_promotion_becomes_a_durable_review(self) -> None:
        client = _WorkflowClient()

        result = await handle_promotion_held({"data": _held()}, client=client)

        assert result == {"status": "SUCCESS"}
        assert len(client.scheduled) == 1, "the hold must become a durable review, not a log line"

    @pytest.mark.asyncio
    async def test_a_redelivered_hold_REATTACHES_instead_of_asking_twice(self) -> None:
        """Dapr redelivers, so a handler may run twice. What must not happen is two reviews of one
        promotion, each asking the approver separately."""
        client = _WorkflowClient()

        first = await handle_promotion_held({"data": _held()}, client=client)
        second = await handle_promotion_held({"data": _held()}, client=client)

        assert first == second == {"status": "SUCCESS"}
        assert len(client.scheduled) == 1

    @pytest.mark.asyncio
    async def test_the_instance_id_is_derived_from_the_TOKEN(self) -> None:
        """The id is what makes re-attach possible, and it is the only handle the door has: nothing
        carries an instance id back from the mover."""
        client = _WorkflowClient()

        await handle_promotion_held({"data": _held()}, client=client)

        assert client.scheduled[0][0] == instance_for("tok-1")

    @pytest.mark.asyncio
    async def test_a_malformed_hold_is_DROPPED_rather_than_retried(self) -> None:
        """The payload is untrusted bus input. A shape that cannot be parsed will not parse on
        redelivery either, so retrying it forever parks a poison message on the topic."""
        client = _WorkflowClient()

        result = await handle_promotion_held({"data": {"nonsense": True}}, client=client)

        assert result == {"status": "DROP"}
        assert client.scheduled == []

    @pytest.mark.asyncio
    async def test_an_engine_outage_RETRIES(self) -> None:
        """Distinct from the malformed case: the event is fine and nothing is watching the promotion.
        Acking that would lose the review entirely."""

        class _Down(_WorkflowClient):
            def schedule_new_workflow(self, **kwargs: Any) -> str:
                raise RuntimeError("connection refused")

            def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> None:
                raise RuntimeError("connection refused")

        result = await handle_promotion_held({"data": _held()}, client=_Down())

        assert result == {"status": "RETRY"}


class TestTheDecisionDoor:
    @pytest.mark.asyncio
    async def test_an_approval_raises_the_event_INTO_the_hosting_app(self) -> None:
        client = _WorkflowClient(instances={instance_for("tok-1"): _State(_held(), WorkflowStatus.RUNNING)})

        result = await decide_promotion(instance_for("tok-1"), approved=True, subject="CiQwOGE4Njg0Yi1kYjg4", client=client)

        assert result["status"] == "accepted"
        assert result["approved"] is True
        assert client.raised == [(instance_for("tok-1"), "promotion_decision", {"approved": True, "subject": "CiQwOGE4Njg0Yi1kYjg4"})]

    @pytest.mark.asyncio
    async def test_a_rejection_is_delivered_too(self) -> None:
        """A no is a decision. Dropping it leaves the promotion to expire, which reads as "nobody
        looked" rather than "somebody said no"."""
        client = _WorkflowClient(instances={instance_for("tok-1"): _State(_held(), WorkflowStatus.RUNNING)})

        await decide_promotion(instance_for("tok-1"), approved=False, subject="CiQwOGE4Njg0Yi1kYjg4", client=client)

        assert client.raised[0][2] == {"approved": False, "subject": "CiQwOGE4Njg0Yi1kYjg4"}

    @pytest.mark.asyncio
    async def test_an_UNKNOWN_instance_is_404_and_never_a_silent_ACCEPT(self) -> None:
        """The failure this whole design exists to avoid. The client accepts the raise regardless, so
        the door must check FIRST — an operator who is told their approval landed, on a promotion that
        then expires, has been lied to by a success."""
        client = _WorkflowClient()

        with pytest.raises(TableNotFoundError):
            await decide_promotion(instance_for("nope"), approved=True, subject="CiQwOGE4Njg0Yi1kYjg4", client=client)

        assert client.raised == [], "the door must not reach the client for an instance it does not host"

    @pytest.mark.asyncio
    async def test_a_TERMINAL_instance_is_refused_with_its_status(self) -> None:
        """The same lie in its commonest form: approving a promotion that already expired. The engine
        accepts the event and discards it, because the instance has completed."""
        client = _WorkflowClient(instances={instance_for("tok-1"): _State(_held(), WorkflowStatus.COMPLETED)})

        with pytest.raises(TableNotFoundError, match="COMPLETED"):
            await decide_promotion(instance_for("tok-1"), approved=True, subject="CiQwOGE4Njg0Yi1kYjg4", client=client)

        assert client.raised == []

    @pytest.mark.asyncio
    async def test_a_decision_naming_NOBODY_is_refused(self) -> None:
        """The subject is recorded into lineage as `promotion_decided_by`. The service-token path of
        the shared auth door returns no subject at all — an unattributable approval is not an approval,
        and the workflow would BLOCK on it anyway, three hops later and unexplained."""
        client = _WorkflowClient(instances={instance_for("tok-1"): _State(_held(), WorkflowStatus.RUNNING)})

        with pytest.raises(PermissionDeniedError):
            await decide_promotion(instance_for("tok-1"), approved=True, subject="", client=client)

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

        await decide_promotion(instance_for("tok-1"), approved=True, subject="alice", client=client, authorize=_authorize)

        assert gated == [("alice", "namespace:acme-gold")], "can_promote is a rung on the DESTINATION stage"

    @pytest.mark.asyncio
    async def test_a_denied_caller_never_reaches_the_workflow(self) -> None:
        client = _WorkflowClient(instances={instance_for("tok-1"): _State(_held(), WorkflowStatus.RUNNING)})

        async def _deny(*, subject: str, obj: str) -> None:
            raise PermissionDeniedError(f"{subject} lacks can_promote on {obj}")

        with pytest.raises(PermissionDeniedError):
            await decide_promotion(instance_for("tok-1"), approved=True, subject="mallory", client=client, authorize=_deny)

        assert client.raised == []

    @pytest.mark.asyncio
    async def test_an_unqualified_destination_is_used_as_is(self) -> None:
        """A projectless estate (#84) has no tenant prefix to add, and inventing one would gate against
        an object no tuple names."""
        client = _WorkflowClient(instances={instance_for("tok-1"): _State(_held(project=""), WorkflowStatus.RUNNING)})
        gated: list[tuple[str, str]] = []

        async def _authorize(*, subject: str, obj: str) -> None:
            gated.append((subject, obj))

        await decide_promotion(instance_for("tok-1"), approved=True, subject="alice", client=client, authorize=_authorize)

        assert gated == [("alice", "namespace:gold")]


class TestTheRungIsValidatorNotAdmin:
    """`can_promote: validator` exists precisely so a non-admin can validate.

    `open_ingest_design.md` §4 rejects a promotion door gated on `can_administer` for exactly this:
    it is "a coarser and different rung from the `can_promote: validator` rung the model already
    defines for exactly this act". The route's first draft reused `authorize_produce`, whose FGA half
    is `can_administer` on the configured project — so the effective gate became admin AND validator,
    and the one person the rung was invented for could not answer.
    """

    def test_the_route_does_not_depend_on_the_ADMIN_gate(self) -> None:
        import inspect

        from medallion.api import promotions

        source = inspect.getsource(promotions)
        assert "authorize_produce" not in source, (
            "the decision route must authenticate WITHOUT the can_administer gate; a project validator "
            "who is not a project admin is exactly who this rung exists for"
        )

    @pytest.mark.asyncio
    async def test_authentication_and_authorization_are_separate_steps(self) -> None:
        """The door proves WHO, the FGA check proves MAY. Fusing them is what lost the rung."""
        import inspect

        from medallion.api.produce_auth import authenticate_subject

        params = inspect.signature(authenticate_subject).parameters
        assert "fga_client" not in params, "authentication must not carry an authorization client"
