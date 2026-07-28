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
from datetime import UTC, datetime
from typing import Any

from dapr.actor import Actor, ActorInterface, actormethod

from annotator.projects.machines import IllegalTransition, may_publish, project_transition
from annotator.projects.models import (
    TERMINAL_TASK_STATES,
    AnnotationProject,
    ProjectState,
    PublishRecord,
    Task,
    TaskState,
)


#: State keys in this actor's partition. Split for the same reason the task actor splits its two:
#: the project document is small and read on every call, the index grows with the corpus.
PROJECT_KEY = "project"
INDEX_KEY = "index"


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

    @actormethod(name="TaskStateChanged")
    async def task_state_changed(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="ListTasks")
    async def list_tasks(self) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="RecordPublish")
    async def record_publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class AnnotationProjectActor(Actor, AnnotationProjectActorInterface):
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

    async def _store(self, project: AnnotationProject, index: dict[str, str] | None = None) -> None:
        project.updated_at = datetime.now(UTC)
        # Counts are DERIVED here, never accumulated. An incremented counter and an index are two
        # truths, and they disagree the first time one write lands and the other does not.
        if index is not None:
            project.counts = _counts(index)
            await self._state_manager.set_state(INDEX_KEY, json.dumps(index))
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
        elif event == "publish":
            project.publish_error = None  # a retry clears the previous failure

        await self._store(project)
        return project.model_dump(mode="json")

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add ONE item to the project as a task, and register it in the index.

        Idempotent on `task_id`: re-sending an item that is already here returns the index unchanged
        rather than resetting a task somebody is holding. The caller seeds the task actor; this
        records that the task belongs to the project.
        """
        project = await self._require()
        if project.state not in {ProjectState.DRAFT, ProjectState.LABELING}:
            raise IllegalTransition("project", project.state, "send")

        task = Task.model_validate(payload)
        index = await self._load_index()
        if task.task_id in index:
            return {"task_id": task.task_id, "created": False, "counts": _counts(index)}

        index[task.task_id] = str(task.state)
        await self._store(project, index=index)
        return {"task_id": task.task_id, "created": True, "counts": _counts(index)}

    async def task_state_changed(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A task reports where it landed. Called by `AnnotationTaskActor` after it persists.

        Unknown task ids are recorded rather than rejected: the alternative is that a task whose
        `send` half-completed becomes permanently invisible to the publish precondition, which is the
        one thing the index exists to prevent.
        """
        task_id = str(payload["task_id"])
        state = TaskState(payload["state"])
        project = await self._require()
        index = await self._load_index()
        index[task_id] = str(state)
        await self._store(project, index=index)
        return {"task_id": task_id, "state": str(state), "counts": project.counts}

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


def _counts(index: dict[str, str]) -> dict[TaskState, int]:
    """Per-state totals over the index. Every state present with a zero, so a consumer reading
    `counts[TaskState.IN_REVIEW]` never has to know whether absence means zero."""
    out = dict.fromkeys(TaskState, 0)
    for raw in index.values():
        out[TaskState(raw)] += 1
    return out
