"""The annotation-project actor — the task index, and what makes publish decidable.

The index is the only place that knows which tasks belong to a project, so these tests are mostly
about one question: can an unreviewed annotation reach silver? Every path that could answer "yes"
is pinned here — a non-terminal task, an empty project, a drifted index, a replayed publish.

Driven against the real actor with a fake state manager, so no sidecar, no placement service and no
database is involved. What they cannot prove is that Dapr routes one project id to one pod; that is
a round-trip property, not a unit-testable one.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import pytest
from annotator.projects.machines import IllegalTransition
from annotator.projects.models import AnnotationProject, ProjectState, Task, TaskState
from annotator.projects.project_actor import INDEX_KEY, PUBLISH_REMINDER, AnnotationProjectActor


class _FakeStateManager:
    """The actor's state partition, in a dict. `try_get_state` returns (found, value) like Dapr's."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def try_get_state(self, key: str) -> tuple[bool, str | None]:
        return (key in self.store, self.store.get(key))

    async def set_state(self, key: str, value: str) -> None:
        self.store[key] = value

    async def save_state(self) -> None:
        return None


class _Actor(AnnotationProjectActor):
    """The real actor with its Dapr plumbing replaced. Reminder calls are RECORDED, not executed —
    `register_reminder` on the real base hits the sidecar's runtime, which unit tests don't have."""

    def __init__(self) -> None:  # noqa: D107 - deliberately bypasses Actor.__init__ (needs a runtime)
        self.sm = _FakeStateManager()
        self._state_manager = cast(Any, self.sm)
        self.reminders_registered: list[str] = []
        self.reminders_unregistered: list[str] = []

    async def register_reminder(self, name: str, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self.reminders_registered.append(name)

    async def unregister_reminder(self, name: str) -> None:  # type: ignore[override]
        self.reminders_unregistered.append(name)


def _project(**kw: Any) -> dict[str, Any]:
    base = AnnotationProject(tenant="acme", slug="charters", state=ProjectState.LABELING).model_dump(mode="json")
    base.update(kw)
    return base


def _item(task_id: str) -> dict[str, Any]:
    return Task(
        task_id=task_id,
        project_id="p1",
        source={"kind": "chunks", "keys": [task_id]},  # type: ignore[arg-type]
        media={"kind": "image", "image_url": f"s3://b/{task_id}.jpg"},  # type: ignore[arg-type]
    ).model_dump(mode="json")


async def _state(actor: _Actor) -> dict[str, Any]:
    """`get()` is Optional by contract. Assert once here rather than suppressing at each call site."""
    project = await actor.get()
    assert project is not None, "the actor lost its project document"
    return project


async def _with_tasks(*states: TaskState) -> _Actor:
    """A frozen project holding one task per state given."""
    actor = _Actor()
    await actor.create(_project())
    for i, state in enumerate(states):
        task_id = f"t{i}"
        await actor.send(_item(task_id))
        await actor.task_state_changed({"task_id": task_id, "state": str(state)})
    await actor.fire({"event": "freeze"})
    return actor


# --------------------------------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_is_idempotent_so_a_retried_request_cannot_reset_a_live_project() -> None:
    actor = _Actor()
    await actor.create(_project())
    await actor.send(_item("t0"))

    again = await actor.create(_project(state=ProjectState.DRAFT))

    assert again["state"] == ProjectState.LABELING, "re-creating reset a project people are labelling in"
    assert (await actor.list_tasks())["total"] == 1, "re-creating dropped the task index"


@pytest.mark.asyncio
async def test_send_is_idempotent_on_task_id() -> None:
    """JetStream redelivers. A repeated send must not double-count or reset the task."""
    actor = _Actor()
    await actor.create(_project())
    first = await actor.send(_item("t0"))
    await actor.task_state_changed({"task_id": "t0", "state": str(TaskState.CLAIMED)})

    second = await actor.send(_item("t0"))

    assert first["created"] is True and second["created"] is False
    listing = await actor.list_tasks()
    assert listing["total"] == 1
    assert listing["tasks"]["t0"] == TaskState.CLAIMED, "a repeated send reset a claimed task"


@pytest.mark.asyncio
async def test_counts_are_derived_from_the_index_not_accumulated() -> None:
    """A counter and an index are two truths. Proven by rewriting the index behind the actor's back:
    the counts must follow the index, because there is no second number to disagree."""
    actor = await _with_tasks(TaskState.ACCEPTED, TaskState.IN_REVIEW)
    actor.sm.store[INDEX_KEY] = json.dumps({"t0": "accepted", "t1": "accepted", "t2": "skipped"})

    listing = await actor.list_tasks()

    assert listing["counts"][TaskState.ACCEPTED] == 2
    assert listing["counts"][TaskState.SKIPPED] == 1
    assert listing["counts"][TaskState.IN_REVIEW] == 0, "an absent state must read as zero, not KeyError"


@pytest.mark.asyncio
async def test_sending_into_a_frozen_project_is_refused() -> None:
    """§5.1: send is legal only in draft/labeling — modelled as absence, not a special case."""
    actor = await _with_tasks(TaskState.ACCEPTED)
    with pytest.raises(IllegalTransition):
        await actor.send(_item("late"))


# --------------------------------------------------------------------------------------------------
# The publish precondition — the answer to "can unreviewed work reach silver?"
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocking",
    [TaskState.UNASSIGNED, TaskState.CLAIMED, TaskState.IN_REVIEW, TaskState.CHANGES_REQUESTED],
)
async def test_one_non_terminal_task_blocks_the_whole_publish(blocking: TaskState) -> None:
    """The owner's "nothing lands before that", enforced. One unreviewed task stops everything."""
    actor = await _with_tasks(TaskState.ACCEPTED, TaskState.ACCEPTED, blocking)

    assert (await actor.list_tasks())["may_publish"] is False
    with pytest.raises(IllegalTransition, match="not all terminal"):
        await actor.fire({"event": "publish"})
    assert (await _state(actor))["state"] == ProjectState.FROZEN


@pytest.mark.asyncio
async def test_all_terminal_permits_the_publish() -> None:
    """`skipped` is terminal too — a skipped item is a decision, not an omission."""
    actor = await _with_tasks(TaskState.ACCEPTED, TaskState.SKIPPED)
    assert (await actor.list_tasks())["may_publish"] is True
    out = await actor.fire({"event": "publish"})
    assert out["state"] == ProjectState.PUBLISHING


@pytest.mark.asyncio
async def test_an_empty_project_cannot_be_published() -> None:
    """`all([])` is True, so the naive precondition would publish an EMPTY dataset and record it as
    provenance. The emptiness check is separate for exactly that reason."""
    actor = _Actor()
    await actor.create(_project())
    await actor.fire({"event": "freeze"})

    assert (await actor.list_tasks())["may_publish"] is True, "may_publish is vacuously true — hence the guard"
    with pytest.raises(IllegalTransition, match="no tasks"):
        await actor.fire({"event": "publish"})


@pytest.mark.asyncio
async def test_a_reopened_task_blocks_a_publish_that_was_previously_allowed() -> None:
    """`reopen` is the one edge that moves a task backwards out of `accepted`. The index must follow
    it — otherwise a project stays publishable while someone is actively changing an annotation."""
    actor = await _with_tasks(TaskState.ACCEPTED, TaskState.ACCEPTED)
    assert (await actor.list_tasks())["may_publish"] is True

    await actor.task_state_changed({"task_id": "t1", "state": str(TaskState.CHANGES_REQUESTED)})

    assert (await actor.list_tasks())["may_publish"] is False


@pytest.mark.asyncio
async def test_a_failed_publish_is_a_resting_state_a_retry_can_leave() -> None:
    actor = await _with_tasks(TaskState.ACCEPTED)
    await actor.fire({"event": "publish"})
    failed = await actor.fire({"event": "publish_failed", "error": "silver write rejected"})
    assert failed["state"] == ProjectState.PUBLISH_FAILED
    assert failed["publish_error"] == "silver write rejected"

    retried = await actor.fire({"event": "publish"})
    assert retried["state"] == ProjectState.PUBLISHING
    assert retried["publish_error"] is None, "a retry kept the previous failure's error"


# --------------------------------------------------------------------------------------------------
# The publish record
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_publish_record_is_written_once_so_a_replay_cannot_contradict_it() -> None:
    """The workflow activity that writes this replays after a crash. A published dataset must not
    acquire a second, disagreeing provenance record."""
    actor = await _with_tasks(TaskState.ACCEPTED)
    await actor.fire({"event": "publish"})
    record = {
        "table_id": "silver$annotations",
        "namespace": "silver",
        "version": 7,
        "publish_id": "pub-1",
        "published_at": "2026-07-28T00:00:00Z",
        "published_by": "gina",
    }

    first = await actor.record_publish(record)
    second = await actor.record_publish({**record, "version": 99, "published_by": "someone-else"})

    assert first["version"] == 7
    assert second["version"] == 7, "a replayed activity overwrote the publish record"
    assert second["published_by"] == "gina"


# --------------------------------------------------------------------------------------------------
# The publish watchdog — the reminder that makes "safe to call again after any crash" have a caller
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_registers_the_watchdog_reminder() -> None:
    """`run_publish` is only crash-safe if something re-invokes it after a crash. The reminder is
    that something: persisted in the actor state store, it survives pod restart and node drain."""
    actor = await _with_tasks(TaskState.ACCEPTED)
    await actor.fire({"event": "publish", "actor": "henry"})
    assert PUBLISH_REMINDER in actor.reminders_registered


@pytest.mark.asyncio
async def test_publish_pins_the_target_namespace_and_trigger_with_the_token() -> None:
    """The endpoint authorizes the TARGET namespace (§6.2 door 2). If the actor does not record it,
    a crash-recovered saga cannot know where the publish was authorized to land — it would have to
    guess, and a guess is an unauthorized write."""
    actor = await _with_tasks(TaskState.ACCEPTED)
    out = await actor.fire({"event": "publish", "actor": "henry", "target_namespace": "gold"})
    assert out["pending_target_namespace"] == "gold"
    assert out["pending_publish_by"] == "henry"


@pytest.mark.asyncio
async def test_a_retry_naming_a_different_namespace_is_refused() -> None:
    """The table id derives from the pinned token+namespace. A retry pointed elsewhere would create
    a SECOND table under a different id while the first may already exist — the exact orphan the
    token exists to prevent. The retry must restate the pinned target or say nothing."""
    actor = await _with_tasks(TaskState.ACCEPTED)
    await actor.fire({"event": "publish", "actor": "henry", "target_namespace": "silver"})
    await actor.fire({"event": "publish_failed", "error": "boom"})

    with pytest.raises(IllegalTransition, match="pending publish targets"):
        await actor.fire({"event": "publish", "actor": "henry", "target_namespace": "gold"})

    retried = await actor.fire({"event": "publish", "actor": "maria", "target_namespace": "silver"})
    assert retried["state"] == ProjectState.PUBLISHING
    assert retried["pending_target_namespace"] == "silver"
    assert retried["pending_publish_by"] == "maria", "the retry must record who drove it"


@pytest.mark.asyncio
async def test_the_reminder_spawns_the_saga_while_publishing_and_unregisters_when_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tick's whole contract: PUBLISHING → run the saga; anything else → stand down."""
    import annotator.projects.lakehouse as lakehouse

    spawned: list[str] = []
    monkeypatch.setattr(lakehouse, "spawn_publish", lambda project_id: spawned.append(project_id))

    actor = await _with_tasks(TaskState.ACCEPTED)
    await actor.fire({"event": "publish", "actor": "henry"})
    await actor.receive_reminder(PUBLISH_REMINDER, b"", timedelta(seconds=1), timedelta(seconds=60))
    assert spawned == [(await _state(actor))["project_id"]]

    await actor.fire({"event": "publish_succeeded"})
    await actor.receive_reminder(PUBLISH_REMINDER, b"", timedelta(seconds=1), timedelta(seconds=60))
    assert len(spawned) == 1, "the reminder ran the saga on a project that is already published"
    assert PUBLISH_REMINDER in actor.reminders_unregistered


@pytest.mark.asyncio
async def test_progress_notes_are_recorded_only_while_publishing() -> None:
    """A4's "not just a spinner": the saga narrates its step onto the project document. A note
    arriving after the project left PUBLISHING is from a stale run and must not scribble on a
    terminal state."""
    actor = await _with_tasks(TaskState.ACCEPTED)
    await actor.fire({"event": "publish", "actor": "henry"})

    noted = await actor.note_progress({"step": "creating table"})
    assert noted["publish_progress"] == "creating table"

    await actor.fire({"event": "publish_succeeded"})
    after = await actor.note_progress({"step": "stale-step"})
    assert after["publish_progress"] is None, "a stale saga note scribbled on a published project"


@pytest.mark.asyncio
async def test_a_publish_retry_clears_the_previous_progress() -> None:
    actor = await _with_tasks(TaskState.ACCEPTED)
    await actor.fire({"event": "publish", "actor": "henry"})
    await actor.note_progress({"step": "tagging version"})
    await actor.fire({"event": "publish_failed", "error": "boom"})

    retried = await actor.fire({"event": "publish", "actor": "henry"})
    assert retried["publish_progress"] is None, "the retry shows the FAILED attempt's last step as if it were running"


# --------------------------------------------------------------------------------------------------
# Consensus v1 — adjudication (the manager's pick, never a blend)
# --------------------------------------------------------------------------------------------------


async def _with_replicas(**states: TaskState) -> _Actor:
    """A labeling project whose index holds the given replica ids at the given states."""
    actor = _Actor()
    await actor.create(_project())
    actor.sm.store[INDEX_KEY] = json.dumps({tid.replace("_", "-"): str(state) for tid, state in states.items()})
    return actor


@pytest.mark.asyncio
async def test_adjudicate_records_the_pick_with_attribution() -> None:
    actor = await _with_replicas(g1_r1=TaskState.ACCEPTED, g1_r2=TaskState.ACCEPTED)

    result = await actor.adjudicate({"group": "g1", "task_id": "g1-r1", "actor": "meg"})

    pick = result["adjudications"]["g1"]
    assert pick["task_id"] == "g1-r1"
    assert pick["by"] == "meg"
    assert pick["at"], "the pick must carry its instant — it is provenance"


@pytest.mark.asyncio
async def test_adjudicate_is_repickable_while_adjudicable() -> None:
    """A manager may change their mind before the publish; the LAST pick stands, restamped."""
    actor = await _with_replicas(g1_r1=TaskState.ACCEPTED, g1_r2=TaskState.ACCEPTED)
    await actor.adjudicate({"group": "g1", "task_id": "g1-r1", "actor": "meg"})

    result = await actor.adjudicate({"group": "g1", "task_id": "g1-r2", "actor": "ines"})

    assert result["adjudications"]["g1"]["task_id"] == "g1-r2"
    assert result["adjudications"]["g1"]["by"] == "ines"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_id", "why"),
    [
        ("g2-r1", "not a replica of the named group"),
        ("g1-r9", "not in this project's index"),
    ],
)
async def test_adjudicate_refuses_a_target_outside_the_group(task_id: str, why: str) -> None:
    actor = await _with_replicas(g1_r1=TaskState.ACCEPTED, g2_r1=TaskState.ACCEPTED)

    with pytest.raises(IllegalTransition):
        await actor.adjudicate({"group": "g1", "task_id": task_id, "actor": "meg"})


@pytest.mark.asyncio
async def test_adjudicate_refuses_a_non_accepted_replica() -> None:
    """A skipped replica carries no labels — canonicalizing one would canonicalize nothing."""
    actor = await _with_replicas(g1_r1=TaskState.SKIPPED, g1_r2=TaskState.ACCEPTED)

    with pytest.raises(IllegalTransition, match="not accepted"):
        await actor.adjudicate({"group": "g1", "task_id": "g1-r1", "actor": "meg"})


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [ProjectState.PUBLISHING, ProjectState.PUBLISHED, ProjectState.ARCHIVED])
async def test_adjudicate_is_refused_once_provenance_is_frozen(state: ProjectState) -> None:
    """Rule 5 extends to picks: an adjudication changed after the publish would describe a facet
    that no longer matches the artifact."""
    actor = await _with_replicas(g1_r1=TaskState.ACCEPTED)
    project = await _state(actor)
    project["state"] = str(state)
    actor.sm.store["project"] = json.dumps(project)

    with pytest.raises(IllegalTransition, match="frozen"):
        await actor.adjudicate({"group": "g1", "task_id": "g1-r1", "actor": "meg"})


@pytest.mark.asyncio
@pytest.mark.parametrize("lookalike", ["g1-rogue", "g1-r1-r2", "g1-rA"])
async def test_adjudicate_refuses_lookalike_ids_that_are_not_the_exact_sibling_shape(lookalike: str) -> None:
    """The audit's bypass shapes: every one of these starts with `g1-r` and could be an indexed,
    accepted task (ids are client-suppliable at send) — only the exact `{group}-r{digits}` shape
    is a member."""
    actor = await _with_replicas(**{lookalike.replace("-", "_"): TaskState.ACCEPTED, "g1_r1": TaskState.ACCEPTED})

    with pytest.raises(IllegalTransition, match="not a replica"):
        await actor.adjudicate({"group": "g1", "task_id": lookalike, "actor": "meg"})


@pytest.mark.asyncio
async def test_a_null_task_id_clears_the_pick_and_clearing_an_absent_pick_is_a_no_op() -> None:
    """The un-wedge path: the publish refuses a stale or groupless pick, so without removal one
    wrong pick would block publishing permanently."""
    actor = await _with_replicas(g1_r1=TaskState.ACCEPTED)
    await actor.adjudicate({"group": "g1", "task_id": "g1-r1", "actor": "meg"})

    cleared = await actor.adjudicate({"group": "g1", "task_id": None, "actor": "meg"})
    assert cleared["adjudications"] == {}

    again = await actor.adjudicate({"group": "g1", "task_id": None, "actor": "meg"})
    assert again["adjudications"] == {}


@pytest.mark.asyncio
async def test_a_pick_is_voided_when_its_target_leaves_accepted() -> None:
    """Reopen → resubmit → re-accept must NOT resurrect a pick made about the earlier content:
    the pick dies the moment the target leaves `accepted`, forcing a fresh, informed pick."""
    actor = await _with_replicas(g1_r1=TaskState.ACCEPTED, g1_r2=TaskState.ACCEPTED)
    await actor.adjudicate({"group": "g1", "task_id": "g1-r1", "actor": "meg"})

    await actor.task_state_changed({"task_id": "g1-r1", "state": "changes_requested"})

    project = await _state(actor)
    assert project["adjudications"] == {}, "the pick survived its target's reopen"

    # …and a later re-accept does not bring it back.
    await actor.task_state_changed({"task_id": "g1-r1", "state": "accepted"})
    assert (await _state(actor))["adjudications"] == {}
