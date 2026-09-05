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

**The identity-bound STATE predicates are a different thing, and they DO live here** (§5.2: is this
subject still the lease holder, is this subject the author of the submission being reviewed, is the
project frozen). The HTTP layer checks them too — against a PRE-TURN snapshot, which is what lets it
answer 403 without spending a turn — but a snapshot is not a precondition: between that read and
this turn the lease can move, the reviewer can become the author, and the transition would then
apply with its precondition already false. Re-evaluated inside the turn the rows cannot move under
the check. Facts this actor cannot compute (`can_manage`, the project's own state) are carried in as
VERIFIED INPUTS the caller states — never as an FGA call from here.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from dapr.actor import Actor, ActorInterface, Remindable, actormethod
from dapr.actor.runtime.failure_policy import ActorReminderFailurePolicy
from pydantic import ValidationError as PydanticValidationError

from annotator.projects.machines import (
    FROZEN_PROJECT_STATES,
    IllegalTransition,
    identity_violation,
    refuse_unreadable_ontology,
    submit_target,
    task_transition,
)
from annotator.projects.models import (
    Draft,
    Link,
    ProjectState,
    ReviewNote,
    Shape,
    Task,
    TaskState,
    Transition,
)
from annotator.projects.ontology import LinkLike, ShapeLike, validate_against_ontology
from annotator.projects.project_actor import AnnotationProjectActorInterface


logger = logging.getLogger(__name__)


#: State keys inside the actor's own store partition. Two documents, not one: the Task is small and
#: read on every call, the Draft can hold hundreds of shapes and is read only while annotating.
TASK_KEY = "task"
DRAFT_KEY = "draft"

#: The lease reminder's name. One per actor, re-registered on each claim, and on each draft save through `save_draft` (renewing the lease) and
#: unregistered the moment the task leaves CLAIMED.
LEASE_REMINDER = "lease"


#: A failed lease-expiry tick is RETRIED, briefly and a bounded number of times.
#:
#: NOT `drop_policy()`, and the difference is the whole reason these are chosen per reminder rather than
#: estate-wide. This reminder is armed with `period=0` — it fires ONCE. Dropping its only tick means the
#: lease never expires and the task stays CLAIMED forever, which is precisely the stranding the reminder
#: exists to prevent. Bounded rather than Dapr's unbounded default, so a permanently poisoned tick still
#: gives up instead of retrying for the life of the actor.
_RETRY_THE_ONE_SHOT: Final = ActorReminderFailurePolicy.constant_policy(interval=timedelta(seconds=10), max_retries=6)


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
        """The task document, or `None` when this actor has never been seeded.

        A document that EXISTS but no longer parses is refused by name — see
        `machines.refuse_unreadable_ontology`. The task carries its OWN captured copy of the
        ontology (that capture is what makes submit-time enforcement possible at all), so this is a
        second, independent seam from the project actor's and needs the same guard.
        """
        has, raw = await self._state_manager.try_get_state(TASK_KEY)
        if not (has and raw):
            return None
        stored = json.loads(raw)
        try:
            return Task.model_validate(stored)
        except PydanticValidationError as exc:
            # Read off the RAW document — there is no model to read it off, and the `task_id` is the
            # id the caller already holds, so the refusal names something it can act on.
            refuse_unreadable_ontology(exc, "task", str(stored.get("task_id") or "unknown"))

    async def _store(self, task: Task) -> None:
        # BOUND THE HISTORY HERE, at the one seam every write goes through, rather than at each
        # `append`: the whole document is re-serialised on every write, so an unbounded trail makes
        # the cost of an event track how many events preceded it. `trim_history` counts what it
        # sheds, so the document never claims a trail it does not carry (docs/DECISIONS.md "The Python estate audit" ANN-11).
        task.trim_history()
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

    async def _ontology_violation(self, task: Task) -> str | None:
        """The submit-time contract check: the DRAFT against the task's CAPTURED ontology.

        No draft submits as an empty shape set — a required class then refuses, which is the honest
        reading of "this task promised those classes".

        Reads the draft's shapes through `ShapeLike` rather than `Shape`: the validator only needs
        id/type/label/attributes, and parsing the full model here would reject a draft for reasons
        that have nothing to do with the task's contract — a geometry the canvas is still editing is
        not a contract violation, and reporting it as one would be a 409 naming the wrong rule.
        """
        raw = await self.get_draft()
        raw_shapes = (raw or {}).get("shapes", [])
        shapes = [ShapeLike.model_validate(s) for s in raw_shapes]
        # Relations have no editor yet (#41), so a draft carries none. Passing the list explicitly
        # keeps the required-relation rule live: an ontology that REQUIRES a link refuses a
        # submission carrying none, which is correct and will stay correct once the editor lands.
        links = [LinkLike.model_validate(link) for link in (raw or {}).get("links", [])]
        return validate_against_ontology(task.ontology, shapes, links)

    @staticmethod
    def _refuse_if_frozen(task: Task, event: str, payload: dict[str, Any], *, principal: bool) -> None:
        """§5.2 rule 5 — nothing escapes a published project — asserted against the project state the
        caller VERIFIED and stated.

        Every principal-caused action must state one. The key is REQUIRED, not merely honoured when
        present: an optional precondition is not a precondition (the lesson `save_draft`'s
        `base_revision` already carries), and a future caller of `fire` that simply did not know
        about rule 5 would otherwise skip it in silence. Its VALUE may be `None` — "I looked, and
        this task's project record is gone" — which is the orphaned-task case the HTTP layer already
        lets through on the transition's own preconditions. System edges state nothing: the only one
        is `lease_expired`, fired by this actor's own reminder, which carries no permission because
        no principal causes it.

        **This narrows the window; it does not close it.** The project's state lives in ANOTHER
        actor, so what arrives here is what the caller observed just before this turn — a freeze
        landing in between is still applied. Closing it would mean reading the project actor from
        inside this turn: a second cross-actor round-trip on every task event, taken while holding
        this task's lock, for a residual the publish saga already covers — it re-reads every task
        from its own actor and refuses a project whose tasks are not all terminal (`saga.collect`,
        `saga._refuse_if_not_terminal`), so a task that moves during `publishing` fails the publish
        rather than riding into silver.
        """
        if not principal:
            return
        if "project_state" not in payload:
            raise IllegalTransition("task", task.state, f"{event} (the caller stated no verified project state)")
        observed = payload["project_state"]
        if observed is None:
            return  # an orphaned task; the transition's own preconditions still apply
        try:
            state = ProjectState(observed)
        except ValueError:
            # Fail closed: an unrecognised state is a caller that cannot have verified rule 5.
            raise IllegalTransition("task", task.state, f"{event} (unrecognised project state {observed!r})") from None
        if state in FROZEN_PROJECT_STATES:
            raise IllegalTransition("task", task.state, f"{event} (project {task.project_id} is {state} — provenance is frozen with the published artifact)")

    async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply one event from `TASK_EDGES`. The transition table is the spec — an edge absent from
        it raises `IllegalTransition`, which is the closed-world guarantee of §5.2 rather than a
        pile of branches here that could drift from the table."""
        event = str(payload["event"])
        actor = payload.get("actor")
        task = await self._require()

        target, permission = task_transition(task.state, event)

        # The identity-bound predicates of §5.2, applied to the rows of THIS turn rather than to the
        # caller's snapshot of them. See the module docstring for why the HTTP layer's copy is not
        # enough; `save_draft` below makes the same move for the same reason.
        violation = identity_violation(
            event=event,
            subject=None if actor is None else str(actor),
            assignee=task.assignee,
            submitted_by=task.submitted_by,
            subject_can_manage=bool(payload.get("subject_can_manage")),
        )
        if violation is not None:
            raise IllegalTransition("task", task.state, f"{event} ({violation})")
        self._refuse_if_frozen(task, event, payload, principal=permission is not None)

        # `submit` is the one edge whose target depends on project config rather than the table.
        # Read from the TASK, captured at send time from the project — never from the payload. A
        # caller-supplied `review_required` would let an annotator self-accept.
        if event == "submit":
            target = submit_target(task.review_required)
            # The ontology's output contract, enforced where it holds for ANY caller (the same
            # placement argument as review_required). The draft this actor holds IS the submission.
            violation = await self._ontology_violation(task)
            if violation is not None:
                raise IllegalTransition("task", task.state, f"submit ({violation})")

        now = datetime.now(UTC)
        # A real Transition, not a dict: appending a raw mapping to a list[Transition] "works" but
        # makes Pydantic emit a serializer warning and writes an untyped row into an append-only
        # audit trail — the one structure that must never acquire a second shape.
        task.transitions.append(Transition(at=now, by=actor or "system", event=event, from_state=task.state, to_state=target))
        task.state = target

        if event == "claim":
            # A self-claim: the assignee IS the actor, by definition. Taking it from the payload
            # would let a caller claim a task on someone else's behalf. The duration falls back to
            # the PROJECT's lease, captured on the task at send — that is what makes
            # `AnnotationProject.lease_seconds` config rather than dead weight.
            task.assignee = actor
            seconds = int(payload.get("lease_seconds") or task.lease_seconds)
            task.lease_expires_at = now + timedelta(seconds=seconds)
            await self._arm_lease(seconds)
        elif event == "assign":
            # A manager assigns to a NAMED user — that is the entire point of the edge, and it is
            # what makes "assign work to an annotator" possible at all. Falling back to `actor`
            # (as this did until 2026-07-28) meant a manager could only ever assign to themselves,
            # silently turning the one manager-driven distribution mechanism into a self-claim.
            task.assignee = str(payload.get("assignee") or actor or "")
            # ASSIGNING pins the task: `lease_expires_at is None` while CLAIMED means never expires
            # (§4.2), because the assignee did not choose when to start.
            task.lease_expires_at = None
        elif event == "save_draft":
            seconds = int(payload.get("lease_seconds") or task.lease_seconds)
            task.lease_expires_at = now + timedelta(seconds=seconds)
            await self._arm_lease(seconds)  # a save RENEWS the lease (so does `save_draft`, the door the canvas uses)
        elif event == "submit":
            task.submitted_by, task.submitted_at = actor, now
            task.assignee, task.lease_expires_at = None, None
        elif event in {"release", "lease_expired", "skip"}:
            task.assignee, task.lease_expires_at = None, None
        elif event in {"accept", "fix_and_accept", "request_changes"}:
            task.reviewed_by, task.reviewed_at = actor, now
            # NO SUPPRESSION HERE, and none is needed. This line carried a mypy-shaped ignore
            # comment, which is both the wrong checker's syntax for this estate and unnecessary:
            # the branch is reached only for the three edges of `SELF_REVIEW_FORBIDDEN`, whose names
            # ARE the `ReviewAction` literals once `accept` is spelled `accepted`, and `ty` narrows
            # to exactly that. A `cast` here reports as redundant.
            task.review_action = "accepted" if event == "accept" else event
            # §5.2: `request_changes` APPENDS a ReviewNote. Without it the reviewer's reason is lost
            # and the annotator is handed the task back with no statement of what to change — which
            # makes the whole changes_requested loop useless in practice.
            if event == "request_changes":
                task.review_notes.append(
                    ReviewNote(
                        by=actor or "system",
                        at=now,
                        action="request_changes",
                        message=str(payload.get("message", "")),
                        shape_ids=[str(s) for s in payload.get("shape_ids", [])],
                    )
                )

        await self._store(task)
        # Disarm only AFTER the store succeeds. The reverse order — disarm, then a store that raises —
        # leaves the PERSISTED state still CLAIMED with its safety net already gone: the lease stops
        # self-expiring and only a human with `can_manage` can un-stick it. Mirror of the project
        # actor's arm-BEFORE-persist rule: arm early, disarm late, so a persisted claim is never
        # uncovered. A re-fired event re-runs disarm safely (`_disarm_lease` suppresses an absent
        # reminder), and a reminder that survives a crash here is absorbed by `receive_reminder`'s
        # stale-check.
        if event in {"assign", "submit", "release", "lease_expired", "skip"}:
            await self._disarm_lease()
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
            from annotator.projects.proxies import typed_proxy  # noqa: PLC0415 - opens a sidecar channel

            proxy = typed_proxy("AnnotationProjectActor", task.project_id, AnnotationProjectActorInterface)
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
        # A draft is writable only while the task is CLAIMED, and that check belongs HERE, not only
        # in the endpoint. The endpoint reads the state and then invokes the actor as two separate
        # steps, so a concurrent `submit` can win the actor's turn in between: the template
        # validates the conforming draft, the task moves to `in_review`, and this write then lands
        # violating shapes into an already-reviewed task — which publish carries into silver.
        # Inside the actor's own serialised turn the state cannot change under the check.
        task = await self._load()
        if task is None:
            raise IllegalTransition("draft", "absent", "save_draft")
        if task.state is not TaskState.CLAIMED:
            raise IllegalTransition("draft", task.state.value, "save_draft")
        # …and only by whoever holds the claim, checked HERE for the same reason the state is. The
        # endpoint's holder check reads a pre-turn snapshot, so a lease that expired and was
        # re-claimed by someone else in between let the OLD holder's save land on the new holder's
        # task — `save_draft` replaces the whole shape set, so that is a silent overwrite of work
        # somebody else is doing. `identity_violation` is the same rule the endpoint applies, read
        # from the one place it is written.
        author = str(payload["author"])
        violation = identity_violation(event="save_draft", subject=author, assignee=task.assignee, submitted_by=task.submitted_by)
        if violation is not None:
            raise IllegalTransition("draft", violation, "save_draft")
        # A draft is a principal action in every sense but the transition table, so rule 5 applies:
        # the sharpest case in the whole plane is a draft landing while the publish saga is reading
        # drafts to build its plan.
        self._refuse_if_frozen(task, "save_draft", payload, principal=True)

        has, raw = await self._state_manager.try_get_state(DRAFT_KEY)
        current = Draft.model_validate(json.loads(raw)) if has and raw else None
        expected = payload.get("base_revision")
        # An OPTIONAL precondition is not a precondition. Skipping the check when `base_revision` is
        # absent made the etag opt-in: two tabs of one annotator both load revision 4, the first
        # saves 30 shapes, the second saves its stale 12 with no `base_revision` — no check, 200, and
        # 18 shapes gone. The design's promise is that two tabs CANNOT lose each other's work, so
        # once a draft exists the caller must state which revision it is editing.
        #
        # The first save is exempt because there is nothing to be stale against (`current is None`).
        if current is not None:
            if expected is None:
                raise IllegalTransition("draft", f"revision {current.revision}", "save without base_revision")
            if int(expected) != current.revision:
                raise IllegalTransition("draft", f"revision {current.revision}", f"save at {expected}")

        draft = Draft(
            task_id=str(payload["task_id"]),
            project_id=str(payload["project_id"]),
            author=str(payload["author"]),
            shapes=[Shape.model_validate(s) for s in payload.get("shapes", [])],
            # Built field by field on purpose (a caller must not be able to set `revision`), which is
            # exactly why an unlisted field is dropped in SILENCE — `links` was, and nothing errored.
            links=[Link.model_validate(link) for link in payload.get("links", [])],
            revision=(current.revision + 1) if current else 1,
            updated_at=datetime.now(UTC),
            origin=payload.get("origin", "human"),
        )
        # A SAVE RENEWS THE LEASE, and it must happen HERE. `fire`'s `save_draft` branch renews too,
        # but nothing reaches it: the canvas calls `PUT /tasks/{id}/draft` -> this method, and
        # `bulk-events.ts` excludes save_draft from the events door in as many words ("save_draft
        # belongs to the canvas"). So the renewal lived on a path the product never takes while this
        # one wrote drafts and touched neither `lease_expires_at` nor the reminder: an annotator
        # saving every 60s for half an hour made 30 successful writes, none of which re-armed
        # anything, and at `lease_seconds` the reminder expired the task out from under her.
        seconds = int(payload.get("lease_seconds") or task.lease_seconds)
        task.lease_expires_at = datetime.now(UTC) + timedelta(seconds=seconds)
        # Arm BEFORE persisting, the same rule `fire` follows. If the store then fails, the reminder
        # is armed against a task that is still CLAIMED — safe. The reverse order strands a claimed
        # task with no self-expiry, which is the hole the lease reminder exists to close.
        await self._arm_lease(seconds)
        await self._state_manager.set_state(DRAFT_KEY, draft.model_dump_json())
        # One save commits both the draft and the renewed task: a draft that persisted without its
        # renewal would put the two back out of step, which is the defect arriving from the far side.
        await self._store(task)
        return draft.model_dump(mode="json")

    async def get_draft(self) -> dict[str, Any] | None:
        has, raw = await self._state_manager.try_get_state(DRAFT_KEY)
        return json.loads(raw) if has and raw else None

    # ---------------------------------------------------------------------------------------------
    # The lease reminder
    # ---------------------------------------------------------------------------------------------

    async def _arm_lease(self, seconds: int) -> None:
        """(Re-)register the expiry callback. `period=0` = fire once, not a repeating timer."""
        await self.register_reminder(LEASE_REMINDER, b"", timedelta(seconds=seconds), timedelta(seconds=0), failure_policy=_RETRY_THE_ONE_SHOT)

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
        # READ THE HOLDER BEFORE THE TRANSITION TAKES IT. `fire()` nulls `assignee` in this same turn
        # (`elif event in {"release", "lease_expired", "skip"}`), so the document it returns can no
        # longer name anybody — this line is the only moment the audience exists.
        holder = task.assignee or ""
        await self.fire({"event": "lease_expired", "actor": None})
        await self._announce_lease_lapse(task.task_id, holder)

    async def _announce_lease_lapse(self, task_id: str, holder: str) -> None:
        """Tell the annotator whose hold just lapsed — the one departure edge no person causes.

        Only a SELF-CLAIMED task can arrive here: `assign` pins `lease_expires_at = None` and never
        expires, and `save_draft` renews the lease. So this names exactly the person who took the work
        off the pool and then went quiet — who has no reason to suspect the task is no longer theirs,
        and whose draft is still sitting against it.

        The emitter comes from the PROCESS holder rather than a dependency: a reminder has no request
        to resolve `ControlEmitterDep` from. `actor` is `system:annotator` because no principal fired
        this; the lane targets on `extra.subject` and never reads `actor`, so that is honesty rather
        than a limitation.

        Best-effort, and that is load-bearing here in a way it is not on the HTTP edges: this runs
        AFTER the transition committed, so an emit that raised would fail the actor turn for a task
        that has already returned to the pool — leaving a lapsed lease reported as an error and,
        worse, retried. Telling somebody must never be able to stop the work being freed.
        """
        if not holder:
            return
        from service_kit.control_emit import emit_control, process_control_emitter

        with suppress(Exception):
            await emit_control(
                process_control_emitter(),
                action="task_lease_expired",
                object_type="annotation_task",
                # The TASK's own id, not `self.id.id`. They are the same value in production (Dapr routes
                # `AnnotationTaskActor/<task_id>`), but reading it off the record keeps this emit
                # independent of the actor runtime — which is what makes it assertable at all.
                object_id=f"annotation_task:{task_id}",
                actor="system:annotator",
                extra={"subject": f"user:{holder}", "event": "lease_expired"},
            )
