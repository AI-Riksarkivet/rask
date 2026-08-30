"""ANN-11 — a task document's append-only history grew without bound, and every event rewrote it.

`Task.transitions` and `Task.review_notes` are `list[...]` with no cap, appended by `fire()`, and
the whole document is re-serialised on every single write (`_store` → `model_dump_json`). So the
cost of an event is proportional to how many events came before it, and the ceiling is "however
long the task lives".

That is not theoretical: `claim` → `release` is a legal loop with no rate limit above it, and a
review can request changes and be re-submitted any number of times. Each pass appends a row that is
never read again except as history, and makes every later write bigger.

The history stays — it is the task's audit trail — but it is BOUNDED, and what falls off the end is
COUNTED rather than silently forgotten, so the document never claims a trail it does not have.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from annotator.projects.actor import TASK_KEY, AnnotationTaskActor
from annotator.projects.models import MAX_REVIEW_NOTES, MAX_TRANSITIONS, ReviewNote, Task, TaskState, Transition


class _FakeStateManager:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def try_get_state(self, key: str) -> tuple[bool, str | None]:
        return (key in self.store, self.store.get(key))

    async def set_state(self, key: str, value: str) -> None:
        self.store[key] = value

    async def save_state(self) -> None:
        return None


class _Actor(AnnotationTaskActor):
    def __init__(self) -> None:  # noqa: D107 - bypasses Actor.__init__, which needs a runtime
        self.sm = _FakeStateManager()
        self._state_manager = cast(Any, self.sm)

    async def register_reminder(self, name: str, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def unregister_reminder(self, name: str) -> None:
        return None


def _task(**kw: Any) -> Task:
    return Task(
        task_id="t1",
        project_id="p1",
        source=cast(Any, {"kind": "chunks", "keys": ["t1"]}),
        media=cast(Any, {"kind": "image", "image_url": "s3://b/t1.jpg"}),
        **kw,
    )


async def _seed(actor: _Actor, task: Task) -> None:
    await actor.seed(task.model_dump(mode="json"))


async def _stored(actor: _Actor) -> Task:
    return Task.model_validate(json.loads(actor.sm.store[TASK_KEY]))


@pytest.mark.asyncio
async def test_a_claim_release_loop_does_not_grow_the_document_forever() -> None:
    """The loop that has no rate limit above it, run past the cap."""
    actor = _Actor()
    await _seed(actor, _task())

    passes = MAX_TRANSITIONS
    for _ in range(passes):
        await actor.fire({"event": "claim", "actor": "dana", "project_state": "labeling"})
        await actor.fire({"event": "release", "actor": "dana", "project_state": "labeling"})

    task = await _stored(actor)
    assert len(task.transitions) == MAX_TRANSITIONS
    assert task.transitions_dropped == passes * 2 - MAX_TRANSITIONS
    # The tail is what a reader wants — "what just happened" — so the OLDEST rows are the ones to go.
    assert task.transitions[-1].event == "release"
    assert task.transitions[0].event in {"claim", "release"}


@pytest.mark.asyncio
async def test_the_serialized_document_stops_growing_once_the_cap_is_reached() -> None:
    """The point of the cap: the cost of an event stops tracking the number of events before it."""
    actor = _Actor()
    await _seed(actor, _task())
    for _ in range(MAX_TRANSITIONS):
        await actor.fire({"event": "claim", "actor": "dana", "project_state": "labeling"})
        await actor.fire({"event": "release", "actor": "dana", "project_state": "labeling"})
    at_cap = len(actor.sm.store[TASK_KEY])

    for _ in range(50):
        await actor.fire({"event": "claim", "actor": "dana", "project_state": "labeling"})
        await actor.fire({"event": "release", "actor": "dana", "project_state": "labeling"})

    assert len(actor.sm.store[TASK_KEY]) <= at_cap


@pytest.mark.asyncio
async def test_an_oversized_document_is_trimmed_by_the_next_write_that_touches_it() -> None:
    """Documents that predate the cap must converge, not stay big forever."""
    now = datetime.now(UTC)
    overfull = _task(
        state=TaskState.UNASSIGNED,
        transitions=[
            Transition(at=now, by="system", event="release", from_state=TaskState.CLAIMED, to_state=TaskState.UNASSIGNED) for _ in range(MAX_TRANSITIONS + 40)
        ],
        review_notes=[ReviewNote(by="rae", at=now, action="request_changes", message=f"note {i}", shape_ids=[]) for i in range(MAX_REVIEW_NOTES + 7)],
    )
    actor = _Actor()
    await _seed(actor, overfull)

    await actor.fire({"event": "claim", "actor": "dana", "project_state": "labeling"})

    task = await _stored(actor)
    assert len(task.transitions) == MAX_TRANSITIONS
    assert len(task.review_notes) == MAX_REVIEW_NOTES
    assert task.transitions_dropped == 41, "the claim's own row counts too — 40 pre-existing plus the one it just appended"
    assert task.review_notes_dropped == 7


@pytest.mark.asyncio
async def test_a_task_under_the_cap_reports_nothing_dropped() -> None:
    """The failure mode that would hide the fix: trimming unconditionally also passes above."""
    actor = _Actor()
    await _seed(actor, _task())
    await actor.fire({"event": "claim", "actor": "dana", "project_state": "labeling"})

    task = await _stored(actor)
    assert len(task.transitions) == 1
    assert task.transitions_dropped == 0
    assert task.review_notes_dropped == 0
