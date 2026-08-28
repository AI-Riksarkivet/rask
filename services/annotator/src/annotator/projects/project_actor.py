"""The annotation-project actor — the second actor, and the one that makes publish decidable.

One virtual actor per project id, holding the project document **and the task index**: which tasks
belong to this project and what state each is in. The index is what turns `may_publish` from a claim
into a check — "nothing lands before every task is terminal" needs somewhere that knows every task.

**Why an index rather than a query.** The obvious alternative is to ask each task actor at publish
time. That is O(tasks) sidecar round-trips on the one operation that must not be slow or flaky, and
it makes the answer depend on every task actor being reachable right then. Keeping the index here
makes the precondition a local read of one document.

**The index is maintained synchronously, and that direction is deliberate.** `AnnotationTaskActor`
calls `TaskStateChanged` on this actor AFTER persisting its own transition. A failure in that call
therefore leaves the index holding the task's OLD state — which is the safe direction for every edge
that moves a task toward terminal (a stale `in_review` BLOCKS publish), and the unsafe direction for
`reopen`, the one edge that moves a task backwards out of `accepted`. That single hole is closed by
the publish workflow re-reading the tasks it is about to publish, so drift can delay a publish but
cannot let an unreviewed annotation into silver.

Counts are derived from the index rather than stored beside it. A stored counter and a stored index
are two truths that disagree the first time one write lands and the other does not.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from dapr.actor import Actor, ActorInterface, Remindable, actormethod
from dapr.actor.runtime.failure_policy import ActorReminderFailurePolicy

from annotator.projects.machines import IllegalTransition, may_publish, project_transition
from annotator.projects.models import (
    TERMINAL_TASK_STATES,
    Adjudication,
    AnnotationProject,
    ProjectState,
    PublishRecord,
    Task,
    TaskState,
    new_id,
)
from annotator.projects.ontology import LabelOntology
from service_kit.lakehouse.warehouse_registry import namespace_for


logger = logging.getLogger(__name__)


#: State keys in this actor's partition. Split for the same reason the task actor splits its two:
#: the project document is small and read on every call, the index grows with the corpus.
PROJECT_KEY = "project"
INDEX_KEY = "index"
#: Task ids the manager REMOVED. A third key rather than a sentinel in the index, because every
#: reader of `index` treats each entry as a live task — `_counts`, the publish precondition and the
#: saga's enumeration would all have to learn the sentinel, and one that forgot would resurrect the
#: task in exactly the place the tombstone exists to protect.
DROPPED_KEY = "dropped"

#: The publish watchdog's name. Registered at the `publish` transition and kept armed (repeating)
#: until the project reaches a terminal state. It is what gives the saga's "safe to call again after
#: any crash" contract a CALLER: the reminder persists in `lance-statestore`, so a pod that dies
#: mid-saga is re-driven on the next tick rather than stranding the project in `publishing` —
#: the state `PROJECT_EDGES` has no principal edge out of.
PUBLISH_REMINDER = "publish-run"

#: First tick ~immediately (the reminder IS the primary trigger, not just crash recovery), then a
#: re-drive each minute until terminal. Every step of the saga is convergent, so an extra tick that
#: races a live run costs a pure read and stands down.
_PUBLISH_DUE = timedelta(seconds=1)
_PUBLISH_PERIOD = timedelta(seconds=60)


#: A failed reminder tick is DISCARDED rather than retried.
#:
#: Correct only because every reminder using it REPEATS: the period is the retry, so a tick that fails
#: is followed by the next one on schedule. Dapr's default is the opposite — a failing callback "will be
#: retried" until the actor unregisters it or is deleted, so a poison tick pins the actor forever at the
#: runtime's own pace.
#:
#: rask already avoided that, by catching everything inside the callback and logging. That works and
#: costs something: the failure never reaches daprd, so it is invisible to the sidecar's metrics and to
#: any dead-letter surface. Declaring the policy says the same thing where an operator can see it.
_DROP_THE_TICK: Final = ActorReminderFailurePolicy.drop_policy()


class AnnotationProjectActorInterface(ActorInterface):
    """The wire surface. The names are the ids the sidecar routes on — renaming one breaks any
    in-flight invocation, so they are fixed here rather than derived from the method names."""

    @actormethod(name="Create")
    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="Get")
    async def get(self) -> dict[str, Any] | None:
        raise NotImplementedError

    @actormethod(name="Fire")
    async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="Send")
    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="SendMany")
    async def send_many(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="TaskStateChanged")
    async def task_state_changed(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="ListTasks")
    async def list_tasks(self) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="RecordPublish")
    async def record_publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="NoteProgress")
    async def note_progress(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="SetOntology")
    async def set_ontology(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="DropTask")
    async def drop_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="Adjudicate")
    async def adjudicate(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class AnnotationProjectActor(Actor, AnnotationProjectActorInterface, Remindable):
    """Holds one project's document and its task index."""

    async def _load(self) -> AnnotationProject | None:
        has, raw = await self._state_manager.try_get_state(PROJECT_KEY)
        return AnnotationProject.model_validate(json.loads(raw)) if has and raw else None

    async def _require(self) -> AnnotationProject:
        project = await self._load()
        if project is None:
            raise IllegalTransition("project", "absent", "any")
        return project

    async def _load_index(self) -> dict[str, str]:
        has, raw = await self._state_manager.try_get_state(INDEX_KEY)
        return dict(json.loads(raw)) if has and raw else {}

    async def _load_dropped(self) -> set[str]:
        has, raw = await self._state_manager.try_get_state(DROPPED_KEY)
        return set(json.loads(raw)) if has and raw else set()

    async def _store(self, project: AnnotationProject, index: dict[str, str] | None = None, dropped: set[str] | None = None) -> None:
        project.updated_at = datetime.now(UTC)
        # Counts are DERIVED here, never accumulated. An incremented counter and an index are two
        # truths, and they disagree the first time one write lands and the other does not.
        if index is not None:
            project.counts = _counts(index)
            await self._state_manager.set_state(INDEX_KEY, json.dumps(index))
        if dropped is not None:
            # Sorted so the persisted bytes are stable — a set's iteration order is not, and an
            # unstable serialisation makes every save look like a change to anything diffing state.
            await self._state_manager.set_state(DROPPED_KEY, json.dumps(sorted(dropped)))
        await self._state_manager.set_state(PROJECT_KEY, project.model_dump_json())
        await self._state_manager.save_state()

    # ---------------------------------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------------------------------

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Idempotent, for the same reason `seed` is: the create request can be retried, and the
        retry must not reset a project people are already labelling in."""
        existing = await self._load()
        if existing is not None:
            return existing.model_dump(mode="json")
        project = AnnotationProject.model_validate(payload)
        await self._store(project, index={})
        return project.model_dump(mode="json")

    async def get(self) -> dict[str, Any] | None:
        project = await self._load()
        return project.model_dump(mode="json") if project else None

    async def set_ontology(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Replace the ontology. The ROUTE owns the authz and state gates; this owns the write.

        Whole-document, and re-validated here rather than trusted from the caller: this actor is
        reachable by any in-cluster caller holding a proxy, so the model's own cross-checks
        (undeclared relation endpoints, duplicate class names) must run on THIS side of the boundary
        too — the same never-trust-the-caller posture as the submit-time contract check.
        """
        project = await self._require()
        project.ontology = LabelOntology.model_validate(payload.get("ontology") or {})
        project.updated_at = datetime.now(UTC)
        await self._store(project)
        return project.model_dump(mode="json")

    async def list_tasks(self) -> dict[str, Any]:
        """The index, plus the publish precondition computed from it. Returned together so a caller
        cannot compute the precondition from a different snapshot than the one it read."""
        index = await self._load_index()
        states = [TaskState(s) for s in index.values()]
        return {
            "tasks": index,
            "counts": _counts(index),
            "total": len(index),
            "terminal": sum(1 for s in states if s in TERMINAL_TASK_STATES),
            "may_publish": may_publish(states),
        }

    # ---------------------------------------------------------------------------------------------
    # Transitions
    # ---------------------------------------------------------------------------------------------

    async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply one project edge from `PROJECT_EDGES`.

        `publish` carries one precondition the table cannot express, because it is a fact about the
        project's TASKS rather than about the project: every task must be terminal (§5.1). Checking
        it here rather than in the API layer means the guarantee holds for any caller — including the
        publish workflow retrying after a crash.
        """
        event = str(payload["event"])
        project = await self._require()

        target, _permission = project_transition(project.state, event)

        if event == "publish":
            index = await self._load_index()
            if not index:
                raise IllegalTransition("project", project.state, "publish (no tasks)")
            if not may_publish([TaskState(s) for s in index.values()]):
                raise IllegalTransition("project", project.state, "publish (tasks are not all terminal)")

        project.state = target
        if event == "publish_failed":
            project.publish_error = str(payload.get("error", "publish failed"))
            # `publish_progress` is KEPT: it names the step that was running when the attempt died.
        elif event == "publish_succeeded":
            project.publish_progress = None
        elif event == "publish":
            project.publish_error = None  # a retry clears the previous failure
            project.publish_progress = None  # ...and the failed attempt's last step
            # Mint the idempotency token ONCE, here, and reuse it on every retry. This is what makes
            # the saga's only non-idempotent step — creating the table — idempotent BY KEY: the table
            # id is derived from this token, so a retry after a crash finds the table it already
            # created rather than making a second one. Minting it per-attempt would produce a fresh
            # table per retry and leave orphans nobody is tracking; minting it at the END (inside
            # `PublishRecord`) would be too late for the step that needs it.
            #
            # This is the "token-keyed idempotent" property `docs/OPERATORS.md` §4 requires of every
            # multi-step path, and it is why this saga needs no workflow engine.
            # The INSTANT is minted with the token and for the same reason: it is written into
            # every published row, so a per-attempt timestamp would make a retry rewrite the dataset
            # with different values than the attempt it is resuming.
            # The door resolves this and sends it explicitly. The fallback is for a payload that
            # names none — and it qualifies by the project's OWN tenant rather than a bare literal,
            # because an unqualified namespace puts every tenant's labels in one place.
            requested_ns = str(payload.get("target_namespace") or namespace_for(project.tenant, "silver"))
            if project.pending_publish_id is None:
                project.pending_publish_id = new_id()
                project.pending_publish_at = datetime.now(UTC)
                # The namespace is PINNED with the token: the table id derives from both, so a retry
                # pointed elsewhere would create a second table for one logical publish.
                project.pending_target_namespace = requested_ns
            elif project.pending_target_namespace and requested_ns != project.pending_target_namespace:
                raise IllegalTransition(
                    "project",
                    project.state,
                    f"publish (pending publish targets {project.pending_target_namespace!r}, not {requested_ns!r} — the retry must restate the pinned target)",
                )
            # Re-stated per attempt: a retry may be driven by someone else, and the record should
            # name the person whose action produced the publish that succeeded.
            project.pending_publish_by = str(payload.get("actor") or "") or project.pending_publish_by
            # Arm the watchdog BEFORE persisting the transition. If registration fails, the fire
            # fails and the project stays frozen — retryable. The reverse order could persist
            # `publishing` with no watchdog, the exact stranding this reminder exists to prevent.
            # (A reminder armed for a store that then fails is benign: the tick sees a non-publishing
            # project and stands down.)
            await self.register_reminder(PUBLISH_REMINDER, b"", _PUBLISH_DUE, _PUBLISH_PERIOD, failure_policy=_DROP_THE_TICK)

        await self._store(project)
        return project.model_dump(mode="json")

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add ONE item to the project as a task, and register it in the index.

        Idempotent on `task_id`: re-sending an item that is already here returns the index unchanged
        rather than resetting a task somebody is holding. The caller seeds the task actor; this
        records that the task belongs to the project.

        A batch of one, delegated rather than reimplemented: two copies of the per-task rules (the
        idempotency answer, the tombstone lift) are two things to keep in step, and the send door
        now drives `send_many` for every request. Kept as a wire method even though nothing in this
        tree calls it, because `Send` is a ROUTING id: a rolling upgrade has old pods invoking it on
        new actors, and withdrawing it in the same release that stops calling it fails those
        in-flight invocations.
        """
        batch = await self.send_many({"tasks": [payload]})
        return {**batch["results"][0], "counts": batch["counts"]}

    async def send_many(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add a BATCH of items to the project as tasks, in ONE turn and ONE state transaction.

        Per task the rules are exactly `send`'s — idempotent on `task_id`, and a re-sent REMOVED id
        lifts its own tombstone (otherwise the new task would be live in the index and permanently
        unable to report where it landed). What the batch changes is the cost and the atomicity.

        **Why a batch rather than a concurrent caller.** Actor reentrancy is disabled estate-wide, so
        N `Send` calls to one project id are serialised by the turn lock however the caller schedules
        them — a `gather` over them buys queueing, not parallelism. And each one is a full
        read-modify-write of the whole index, so N of them push O(N²) bytes through the state store
        in N save transactions. The send door admits 1000 replicas in one request; only collapsing
        them into a single load/mutate/save makes that affordable (open_python-audit ANN-03).

        **The state gate is evaluated once, up front**, over the whole batch: a project that stops
        accepting items part-way through one request has no meaning, and a partial refusal would be
        the half-populated project the door already refuses to create.

        Nothing is written when nothing changed, so a re-send of an already-indexed batch is a pure
        read — the same property `send` had, and what makes a re-send safe to retry blindly.
        """
        project = await self._require()
        if project.state not in {ProjectState.DRAFT, ProjectState.LABELING}:
            raise IllegalTransition("project", project.state, "send")

        tasks = [Task.model_validate(raw) for raw in payload.get("tasks") or []]
        index = await self._load_index()
        dropped = await self._load_dropped()
        results: list[dict[str, Any]] = []
        lifted = False
        for task in tasks:
            if task.task_id in index:
                results.append({"task_id": task.task_id, "created": False})
                continue
            index[task.task_id] = str(task.state)
            if task.task_id in dropped:
                dropped.discard(task.task_id)
                lifted = True
            results.append({"task_id": task.task_id, "created": True})

        if any(row["created"] for row in results):
            await self._store(project, index=index, dropped=dropped if lifted else None)
        return {"results": results, "counts": _counts(index)}

    async def task_state_changed(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A task reports where it landed. Called by `AnnotationTaskActor` after it persists.

        Unknown task ids are recorded rather than rejected: the alternative is that a task whose
        `send` half-completed becomes permanently invisible to the publish precondition, which is the
        one thing the index exists to prevent.

        A DROPPED id is the exception, and it has to be, because `drop_task` deliberately leaves the
        task's own actor alone. That actor is not inert: it keeps an armed `lease` reminder and calls
        `_report_state` on every transition. So a claimed-then-dropped task reported itself back into
        the index roughly half an hour later, `may_publish` went false again, and `fire("publish")`
        refused with nothing naming the task the manager had already removed. Worse if the tick landed
        during PUBLISHING: the saga enumerated it and failed a legal publish.

        "Unknown" and "removed" are different answers, and only the tombstone can tell them apart.
        """
        task_id = str(payload["task_id"])
        state = TaskState(payload["state"])
        project = await self._require()
        dropped = await self._load_dropped()
        if task_id in dropped:
            return {"task_id": task_id, "state": str(state), "counts": project.counts, "dropped": True}
        index = await self._load_index()
        index[task_id] = str(state)
        # A canonical pick is void the moment its target leaves `accepted` (audit finding: a pick
        # silently surviving reopen → resubmit → re-accept would canonicalize content the
        # adjudicator never saw). Dropping it forces a fresh, informed pick; the publish-side
        # refusal remains the backstop for the report-failure window this cannot see.
        for group, adjudication in list(project.adjudications.items()):
            if adjudication.task_id == task_id and state is not TaskState.ACCEPTED:
                del project.adjudications[group]
        await self._store(project, index=index)
        return {"task_id": task_id, "state": str(state), "counts": project.counts}

    async def drop_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove a task from the index. The ROUTE owns authz and the state gate; this owns the write.

        Removal exists because a send can put items into a project that can never be completed — an
        item naming a dataset that was renamed or removed is the one we hit, and before this the only
        way past it was to abandon the project. The publish precondition requires EVERY task terminal,
        so one unfinishable item blocks the whole thing forever.

        The task's own actor is left alone deliberately. This index is what the precondition reads and
        what the publish enumerates; an orphaned task document is inert, whereas reaching across to
        delete it would be a second write that can half-fail and leave the index disagreeing with
        reality. Dropping the INDEX entry is the whole of what "removed" means here.

        An adjudication pointing at the dropped task goes with it — the same reasoning as
        `task_state_changed`: a pick whose target no longer exists would canonicalize nothing.

        A TOMBSTONE goes with it too. The orphaned actor still reports its transitions, and without a
        record that this id was removed on purpose, `task_state_changed` puts it straight back.
        """
        task_id = str(payload["task_id"])
        project = await self._require()
        index = await self._load_index()
        if task_id not in index:
            # Idempotent: a retried removal must not 404 a project that is already in the state the
            # caller asked for.
            return {"task_id": task_id, "removed": False, "total": len(index)}
        del index[task_id]
        dropped = await self._load_dropped() | {task_id}
        for group, adjudication in list(project.adjudications.items()):
            if adjudication.task_id == task_id:
                del project.adjudications[group]
        await self._store(project, index=index, dropped=dropped)
        return {"task_id": task_id, "removed": True, "total": len(index)}

    async def record_publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write the publish record. Set ONCE (§4.1): a second call is a no-op returning the first.

        Idempotent because the publish workflow can replay this activity after a crash, and a
        published dataset must not acquire a second, contradictory provenance record.
        """
        project = await self._require()
        if project.published is not None:
            return project.published.model_dump(mode="json")
        project.published = PublishRecord.model_validate(payload)
        await self._store(project)
        return project.published.model_dump(mode="json")

    async def note_progress(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record the saga's current step (A4's "not just a spinner").

        Accepted only while PUBLISHING: a note arriving after the project reached a terminal state
        is from a stale run and must not scribble on it. No-op rather than error — the saga's
        progress narration is best-effort by design and must never fail a publish.
        """
        project = await self._require()
        if project.state is ProjectState.PUBLISHING:
            project.publish_progress = str(payload.get("step") or "")
            await self._store(project)
        return project.model_dump(mode="json")

    async def adjudicate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record — or, with a null `task_id`, CLEAR — the manager's canonical pick for one replica
        group (consensus v1's merge step).

        A pick, never a blend — see `Adjudication`. Re-pickable while the project is still
        adjudicable (labeling/frozen); refused once provenance is frozen with the artifact
        (publishing/published/archived), for the same reason every task edge is. The target must
        match the EXACT deterministic sibling shape (`{group}-r{k}`) — a bare prefix check let a
        client-chosen id like `g1-r1-r2` or `g1-rogue` pass as a member of `g1` (audit finding) —
        and be an indexed, ACCEPTED task: a skipped replica carries no labels, so canonicalizing
        one would canonicalize nothing.

        Clearing exists because a pick is the ONLY project field with no other exit: `publish`
        refuses a stale or groupless pick (correctly), so without removal one wrong pick would
        wedge the publish permanently. Clearing an absent pick is a no-op, not an error.

        The index this validates against can lag a task's own actor (the report is best-effort) —
        the safe direction: the publish re-reads every task and refuses a pick that is no longer
        accepted, and `task_state_changed` voids a pick the moment its target leaves `accepted`.
        """
        import re  # noqa: PLC0415 - only this method needs it

        from annotator.projects.machines import FROZEN_PROJECT_STATES  # noqa: PLC0415 - avoids widening the module import surface

        group = str(payload["group"])
        project = await self._require()
        if project.state in FROZEN_PROJECT_STATES:
            raise IllegalTransition("project", project.state, "adjudicate (provenance is frozen with the published artifact)")

        raw_task_id = payload.get("task_id")
        if raw_task_id is None or raw_task_id == "":
            project.adjudications.pop(group, None)
            await self._store(project)
            return project.model_dump(mode="json")

        task_id = str(raw_task_id)
        if re.fullmatch(rf"{re.escape(group)}-r\d+", task_id) is None:
            raise IllegalTransition("project", project.state, f"adjudicate ({task_id} is not a replica of group {group})")
        index = await self._load_index()
        state = index.get(task_id)
        if state is None:
            raise IllegalTransition("project", project.state, f"adjudicate ({task_id} is not in this project)")
        if TaskState(state) is not TaskState.ACCEPTED:
            raise IllegalTransition("project", project.state, f"adjudicate ({task_id} is {state}, not accepted — only accepted work can be canonical)")

        project.adjudications[group] = Adjudication(task_id=task_id, by=str(payload.get("actor") or ""), at=datetime.now(UTC))
        await self._store(project)
        return project.model_dump(mode="json")

    # ---------------------------------------------------------------------------------------------
    # The publish watchdog
    # ---------------------------------------------------------------------------------------------

    async def receive_reminder(self, name: str, state: bytes, due_time: timedelta, period: timedelta, ttl: timedelta | None = None) -> None:
        """One watchdog tick: PUBLISHING → drive the saga; anything else → stand down.

        The saga is NOT awaited here. Actor turns are serialised, and `run_publish` calls back into
        this very actor through its proxy surface (`get`/`list_tasks`/`fire`/`record_publish`) — an
        in-turn await would deadlock on our own mailbox. `spawn_publish` schedules it on the event
        loop and the turn ends; the spawned task then proxies a free actor.
        """
        if name != PUBLISH_REMINDER:
            return
        project = await self._load()
        if project is None or project.state is not ProjectState.PUBLISHING:
            # Terminal (or gone): the watchdog has nothing left to guard. An absent reminder is
            # possible here (a previous tick already unregistered), so suppression is of an
            # expected condition, not a swallow.
            with suppress(Exception):
                await self.unregister_reminder(PUBLISH_REMINDER)
            return
        from annotator.projects import lakehouse  # noqa: PLC0415 - the actor module must import without the SDK

        lakehouse.spawn_publish(project.project_id)


def _counts(index: dict[str, str]) -> dict[TaskState, int]:
    """Per-state totals over the index. Every state present with a zero, so a consumer reading
    `counts[TaskState.IN_REVIEW]` never has to know whether absence means zero."""
    out = dict.fromkeys(TaskState, 0)
    for raw in index.values():
        out[TaskState(raw)] += 1
    return out
