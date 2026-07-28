"""The annotation-task actor — S6, and the first registered actor in the estate.

One virtual actor per task id. Dapr routes `AnnotationTaskActor/<task_id>` to exactly one pod
cluster-wide and serialises calls to it, so **turn-based concurrency IS the lock**: two annotators
clicking Claim on the same task are serialised by the runtime, with no advisory lock, no
`SELECT … FOR UPDATE`, and no leader election. That guarantee is a property of the platform rather
than of the replica count — which matters because `chart/values.yaml` runs stateless services at
`replicas: 1` today and says "scale freely in prod". An in-process lock would appear to work now and
silently protect nothing the moment that happens.

**Do not add a distributed-lock component for this.** Needing one would mean the actor boundary is
drawn in the wrong place.

Leases are **reminders**, not a cron sweep. `register_reminder` persists in `lance-statestore`, so it
survives pod restart, rescheduling and node drain, and costs O(expiries) rather than O(tasks) — a
sweeper would rescan every task to find the few that expired. The domain core predicted this before
the mechanism existed: `TASK_EDGES[(CLAIMED, "lease_expired")]` is the only task edge whose
permission is `None`, because no principal fires it.

**Authorization is NOT performed here.** `annotator.projects.machines` says which `can_*` an event
requires and the HTTP layer checks it against OpenFGA before invoking. Keeping the split means the
actor stays testable without OpenFGA, and the op→privilege map has exactly one home.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from dapr.actor import Actor, ActorInterface, Remindable, actormethod

from annotator.projects.machines import IllegalTransition, submit_target, task_transition
from annotator.projects.models import Draft, Shape, Task, TaskState, Transition
from annotator.projects.project_actor import AnnotationProjectActorInterface


logger = logging.getLogger(__name__)


#: State keys inside the actor's own store partition. Two documents, not one: the Task is small and
#: read on every call, the Draft can hold hundreds of shapes and is read only while annotating.
TASK_KEY = "task"
DRAFT_KEY = "draft"

#: The lease reminder's name. One per actor, re-registered on each claim/save (renewing the lease) and
#: unregistered the moment the task leaves CLAIMED.
LEASE_REMINDER = "lease"


class AnnotationTaskActorInterface(ActorInterface):
    """The wire surface. Dapr requires an explicit interface — the method names here are the ids the
    sidecar routes on, so renaming one is a breaking change to any in-flight invocation.

    The bodies raise rather than being `...`: they are never executed (the concrete actor overrides
    every one, and `@actormethod` only registers the name), but a declaration still has to satisfy
    its own return type or `ty` reports it as always returning None."""

    @actormethod(name="Seed")
    async def seed(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="Get")
    async def get(self) -> dict[str, Any] | None:
        raise NotImplementedError

    @actormethod(name="Fire")
    async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="SaveDraft")
    async def save_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="GetDraft")
    async def get_draft(self) -> dict[str, Any] | None:
        raise NotImplementedError


class AnnotationTaskActor(Actor, AnnotationTaskActorInterface, Remindable):
    """Holds one task's state and its draft."""

    async def _load(self) -> Task | None:
        has, raw = await self._state_manager.try_get_state(TASK_KEY)
        return Task.model_validate(json.loads(raw)) if has and raw else None

    async def _store(self, task: Task) -> None:
        await self._state_manager.set_state(TASK_KEY, task.model_dump_json())
        await self._state_manager.save_state()

    async def _require(self) -> Task:
        task = await self._load()
        if task is None:
            raise IllegalTransition("task", "absent", "any")
        return task

    # ---------------------------------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------------------------------

    async def seed(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send an item into the project — the actor's birth. Idempotent: re-seeding an existing task
        returns what is already there rather than resetting a claim someone is holding."""
        existing = await self._load()
        if existing is not None:
            return existing.model_dump(mode="json")
        task = Task.model_validate(payload)
        await self._store(task)
        return task.model_dump(mode="json")

    async def get(self) -> dict[str, Any] | None:
        task = await self._load()
        return task.model_dump(mode="json") if task else None

    async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply one event from `TASK_EDGES`. The transition table is the spec — an edge absent from
        it raises `IllegalTransition`, which is the closed-world guarantee of §5.2 rather than a
        pile of branches here that could drift from the table."""
        event = str(payload["event"])
        actor = payload.get("actor")
        task = await self._require()

        target, _permission = task_transition(task.state, event)

        # `submit` is the one edge whose target depends on project config rather than the table.
        # Read from the TASK, captured at send time from the project — never from the payload. A
        # caller-supplied `review_required` would let an annotator self-accept.
        if event == "submit":
            target = submit_target(task.review_required)

        now = datetime.now(UTC)
        # A real Transition, not a dict: appending a raw mapping to a list[Transition] "works" but
        # makes Pydantic emit a serializer warning and writes an untyped row into an append-only
        # audit trail — the one structure that must never acquire a second shape.
        task.transitions.append(Transition(at=now, by=actor or "system", event=event, from_state=task.state, to_state=target))
        task.state = target

        if event in {"claim", "assign"}:
            task.assignee = actor
            # A manager ASSIGNING pins the task: `lease_expires_at is None` while CLAIMED means
            # never expires (§4.2). A self-claim takes a lease and arms the reminder.
            if event == "claim":
                seconds = int(payload.get("lease_seconds", 1800))
                task.lease_expires_at = now + timedelta(seconds=seconds)
                await self._arm_lease(seconds)
            else:
                task.lease_expires_at = None
                await self._disarm_lease()
        elif event == "save_draft":
            seconds = int(payload.get("lease_seconds", 1800))
            task.lease_expires_at = now + timedelta(seconds=seconds)
            await self._arm_lease(seconds)  # a save RENEWS the lease
        elif event == "submit":
            task.submitted_by, task.submitted_at = actor, now
            task.assignee, task.lease_expires_at = None, None
            await self._disarm_lease()
        elif event in {"release", "lease_expired", "skip"}:
            task.assignee, task.lease_expires_at = None, None
            await self._disarm_lease()
        elif event in {"accept", "fix_and_accept", "request_changes"}:
            task.reviewed_by, task.reviewed_at = actor, now
            task.review_action = "accepted" if event == "accept" else event  # type: ignore[assignment]

        await self._store(task)
        await self._report_state(task)
        return task.model_dump(mode="json")

    async def _report_state(self, task: Task) -> None:
        """Tell the project actor where this task landed, so the publish precondition stays decidable.

        AFTER the store, never before. If this call fails the index holds the task's OLD state, and
        for every edge that moves a task toward terminal that is the safe direction — a stale
        `in_review` BLOCKS publish. Reporting first would invert that: the index would claim
        `accepted` for a task whose own actor never persisted it.

        Non-fatal on purpose. An annotator's submit must not fail because the project actor is
        briefly unreachable; the cost is a delayed publish, and the publish workflow re-reads the
        tasks it is about to commit, so a stale index cannot let unreviewed work into silver.
        """
        try:
            from dapr.actor import ActorId, ActorProxy  # noqa: PLC0415 - opens a sidecar channel

            proxy = ActorProxy.create(
                "AnnotationProjectActor",
                ActorId(task.project_id),
                AnnotationProjectActorInterface,
            )
            await proxy.task_state_changed({"task_id": task.task_id, "state": str(task.state)})
        except Exception:
            logger.warning(
                "task %s could not report %s to project %s — the index is stale until its next transition",
                task.task_id,
                task.state,
                task.project_id,
            )

    # ---------------------------------------------------------------------------------------------
    # Draft — one document per (task, author), not a row per shape (§4.3)
    # ---------------------------------------------------------------------------------------------

    async def save_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Replace the whole shape set in ONE keyed write, bumping `revision`.

        The write-amplification fix made structural: N shapes are one write, not N rows. Two tabs of
        the same annotator cannot silently lose each other's work — the actor serialises them, and
        `revision` is the etag the caller compares.
        """
        has, raw = await self._state_manager.try_get_state(DRAFT_KEY)
        current = Draft.model_validate(json.loads(raw)) if has and raw else None
        expected = payload.get("base_revision")
        if current is not None and expected is not None and int(expected) != current.revision:
            raise IllegalTransition("draft", f"revision {current.revision}", f"save at {expected}")

        draft = Draft(
            task_id=str(payload["task_id"]),
            project_id=str(payload["project_id"]),
            author=str(payload["author"]),
            shapes=[Shape.model_validate(s) for s in payload.get("shapes", [])],
            revision=(current.revision + 1) if current else 1,
            updated_at=datetime.now(UTC),
            origin=payload.get("origin", "human"),
        )
        await self._state_manager.set_state(DRAFT_KEY, draft.model_dump_json())
        await self._state_manager.save_state()
        return draft.model_dump(mode="json")

    async def get_draft(self) -> dict[str, Any] | None:
        has, raw = await self._state_manager.try_get_state(DRAFT_KEY)
        return json.loads(raw) if has and raw else None

    # ---------------------------------------------------------------------------------------------
    # The lease reminder
    # ---------------------------------------------------------------------------------------------

    async def _arm_lease(self, seconds: int) -> None:
        """(Re-)register the expiry callback. `period=0` = fire once, not a repeating timer."""
        await self.register_reminder(LEASE_REMINDER, b"", timedelta(seconds=seconds), timedelta(seconds=0))

    async def _disarm_lease(self) -> None:
        """Unregister on any edge out of CLAIMED. Leaving it armed would expire a task that is
        already submitted — the reminder outlives the state it was guarding."""
        # An absent reminder is the NORMAL case here (every edge out of CLAIMED calls this, including
        # ones that never armed one), so this is suppression of an expected condition, not a swallow.
        with suppress(Exception):
            await self.unregister_reminder(LEASE_REMINDER)

    async def receive_reminder(self, name: str, state: bytes, due_time: timedelta, period: timedelta, ttl: timedelta | None = None) -> None:
        """The lease expired. System-caused: `TASK_EDGES` gives this edge no permission because no
        principal fires it. The DRAFT IS KEPT — an annotator whose lease lapsed has not lost work."""
        if name != LEASE_REMINDER:
            return
        task = await self._load()
        if task is None or task.state is not TaskState.CLAIMED:
            await self._disarm_lease()  # stale reminder for a task that already moved on
            return
        await self.fire({"event": "lease_expired", "actor": None})
