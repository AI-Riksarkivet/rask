"""A send makes ONE index write, and every seed lands before it (open_python-audit ANN-03).

`POST /projects/{id}/items` admits `len(items) × consensus_n` up to 1000 replicas, and it used to
make TWO sequential sidecar round-trips per replica — seed the task actor, then `Send` the project
actor — so a full send was 2000 serialised RPCs in one HTTP request, past any ingress timeout.

Round-trip COUNT is only half of it, and the half a `gather` cannot fix. Actor reentrancy is
disabled estate-wide, so every `Send` to one project id queues on that actor's turn lock however the
caller schedules them; and each `Send` is a read-modify-write of the WHOLE index, so N sends push
O(N²) bytes through the state store in N save transactions. Only a batch removes that, which is why
these tests pin the number of project-actor calls rather than the overlap between them: overlap is
what a `Semaphore`+`gather` band-aid would fake.

The seeds are a different case and DO parallelise — each addresses its own task actor id — so those
are pinned for overlap.

What the batch tightens, and is pinned here so it cannot be lost again: a mid-send ownership
refusal now leaves ZERO index entries instead of every replica up to the offending one.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import project_events as ev
from annotator.projects.machines import IllegalTransition
from annotator.projects.models import AnnotationProject, ProjectState, Task, TaskState
from annotator.projects.project_actor import DROPPED_KEY, INDEX_KEY, AnnotationProjectActor
from service_kit.exceptions import register_handlers
from service_kit.media.deps import get_state


SUBJECT = "henry"


class _Log:
    """One ordered call log shared by both fake actors, plus a live/peak pair for the seed fan-out.

    Ordering and concurrency are both properties of the HANDLER rather than of either actor, so they
    can only be observed from something both actors write into.
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.live = 0
        self.peak = 0


class _FakeProject:
    """The project actor. Implements BOTH doors so the tests can tell which one the handler used —
    a fake carrying only `send_many` would fail the old code with an AttributeError rather than with
    the count these tests are about."""

    def __init__(self, log: _Log, state: ProjectState = ProjectState.LABELING) -> None:
        self.log = log
        self.state = state
        #: Per-task `Send` calls — the shape being retired.
        self.sent: list[dict[str, Any]] = []
        #: `SendMany` calls, each the whole batch it was handed.
        self.batches: list[list[dict[str, Any]]] = []
        self.index: dict[str, str] = {}

    async def get(self) -> dict[str, Any] | None:
        return {"state": str(self.state), "project_id": "p1", "review_required": True, "lease_seconds": 900}

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.log.events.append("send")
        self.sent.append(payload)
        created = payload["task_id"] not in self.index
        self.index[payload["task_id"]] = payload["state"]
        return {"task_id": payload["task_id"], "created": created, "counts": {}}

    async def send_many(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.log.events.append("send_many")
        tasks = list(payload["tasks"])
        self.batches.append(tasks)
        results = []
        for body in tasks:
            created = body["task_id"] not in self.index
            self.index[body["task_id"]] = body["state"]
            results.append({"task_id": body["task_id"], "created": created})
        return {"results": results, "counts": {}}


class _FakeTask:
    """The task actor. `seed` is idempotent and returns what is ALREADY there, which is how a
    cross-project send is detectable at all; `owners` names the tasks that already belong elsewhere.

    It yields once inside the call so overlapping seeds are observable without a sleep race.
    """

    def __init__(self, log: _Log, owners: dict[str, str] | None = None) -> None:
        self.log = log
        self.seeded: list[dict[str, Any]] = []
        self.owners = owners or {}

    async def seed(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.log.live += 1
        self.log.peak = max(self.log.peak, self.log.live)
        self.log.events.append("seed")
        await asyncio.sleep(0)
        self.log.live -= 1
        self.seeded.append(payload)
        return {**payload, "project_id": self.owners.get(payload["task_id"], payload["project_id"])}


def _client(project: _FakeProject, task: _FakeTask, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def checker(*, user: str, relation: str, obj: str) -> bool:
        return relation == "can_send_items"

    monkeypatch.setattr(ev, "_project_proxy", lambda _p: project)
    monkeypatch.setattr(ev, "_task_proxy", lambda _t: task)

    app = FastAPI()
    register_handlers(app)
    app.include_router(ev.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: SUBJECT
    # The send door consults the dataset registry; real app state is built by the lifespan and does
    # not exist here. No test in this file names a dataset, so every item takes the default.
    app.dependency_overrides[get_state] = lambda: SimpleNamespace(registry=SimpleNamespace(list_ids=list))
    return TestClient(app)


def _items(n: int) -> list[dict[str, Any]]:
    return [{"task_id": f"i{k}", "source": {"kind": "chunks", "keys": [f"k{k}"]}, "media": {"kind": "image"}} for k in range(n)]


# --------------------------------------------------------------------------------------------------
# The endpoint: one index write per send, after every seed
# --------------------------------------------------------------------------------------------------


def test_a_multi_item_send_writes_the_index_ONCE(monkeypatch: pytest.MonkeyPatch) -> None:
    """25 items must cost 25 seeds (each its own actor id) and exactly ONE project-actor call.

    This is the assertion a bounded-`gather` fix cannot pass, and that is the point: the project
    actor serialises every call to one id anyway, and each per-task `Send` rewrites the whole index.
    """
    log = _Log()
    project, task = _FakeProject(log), _FakeTask(log)
    client = _client(project, task, monkeypatch)

    response = client.post("/projects/p1/items", json={"items": _items(25)})

    assert response.status_code == 201, response.text
    assert len(task.seeded) == 25, "every replica must still be seeded"
    assert project.sent == [], "the per-task Send door must no longer be used by the send path"
    assert len(project.batches) == 1, f"the index was written {len(project.batches) or len(project.sent)} times, not once"
    assert [body["task_id"] for body in project.batches[0]] == [f"i{k}" for k in range(25)]
    assert response.json() == {"sent": 25, "created": 25, "task_ids": [f"i{k}" for k in range(25)]}


def test_every_seed_lands_before_the_index_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module's correctness argument, now global rather than per item: a crash mid-send leaves
    tasks that exist but are not indexed (invisible to the publish precondition, repaired by an
    idempotent re-send), never an index entry for a task whose actor was never seeded.

    Nothing pinned this ordering before, so a batch that indexed first would have passed every
    existing test.
    """
    log = _Log()
    project, task = _FakeProject(log), _FakeTask(log)
    client = _client(project, task, monkeypatch)

    client.post("/projects/p1/items", json={"items": _items(8)})

    assert log.events == ["seed"] * 8 + ["send_many"], f"seeds and the index write interleave: {log.events}"


def test_the_seeds_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seeds address DIFFERENT task actor ids, so unlike the index write they genuinely parallelise.

    Deterministic rather than timed: each seed yields once, so a sequential loop can only ever have
    one call in flight.
    """
    log = _Log()
    project, task = _FakeProject(log), _FakeTask(log)
    client = _client(project, task, monkeypatch)

    client.post("/projects/p1/items", json={"items": _items(8)})

    assert log.peak > 1, "the seeds were awaited one at a time"


def test_a_foreign_task_mid_send_leaves_NO_index_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ownership refusal, tightened. Indexing a task that belongs to another project freezes its
    entry at the seeded value forever — the task's own actor only ever reports to its real owner — so
    `may_publish` would be false for the life of the project.

    Refusing per item left every replica BEFORE the offender already indexed, which is that same
    permanent wedge for a prefix of the send. One batch after all the seeds makes the refusal atomic.
    """
    log = _Log()
    project, task = _FakeProject(log), _FakeTask(log, owners={"i3": "someone-elses-project"})
    client = _client(project, task, monkeypatch)

    response = client.post("/projects/p1/items", json={"items": _items(5)})

    assert response.status_code == 409, response.text
    assert "already belongs to project someone-elses-project" in response.json()["detail"]
    assert project.batches == [] and project.sent == [], f"a refused send indexed {len(project.sent) or len(project.batches)} write(s)"


def test_the_refusal_names_the_FIRST_offender_in_send_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seeds are gathered, so completion order is not send order. The message must name the same
    task on every run or the refusal is unreproducible."""
    log = _Log()
    owners = {"i1": "other-a", "i4": "other-b"}
    project, task = _FakeProject(log), _FakeTask(log, owners=owners)
    client = _client(project, task, monkeypatch)

    response = client.post("/projects/p1/items", json={"items": _items(6)})

    assert response.status_code == 409, response.text
    assert "task i1 already belongs" in response.json()["detail"], response.json()["detail"]


# --------------------------------------------------------------------------------------------------
# The actor: SendMany is Send's contract, applied in one turn and one save
# --------------------------------------------------------------------------------------------------


class _FakeStateManager:
    """The actor's state partition, in a dict, counting the saves — the cost the batch removes."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.saves = 0

    async def try_get_state(self, key: str) -> tuple[bool, str | None]:
        return (key in self.store, self.store.get(key))

    async def set_state(self, key: str, value: str) -> None:
        self.store[key] = value

    async def save_state(self) -> None:
        self.saves += 1


class _Actor(AnnotationProjectActor):
    """The real actor with its Dapr plumbing replaced."""

    def __init__(self) -> None:  # noqa: D107 - deliberately bypasses Actor.__init__ (needs a runtime)
        self.sm = _FakeStateManager()
        self._state_manager = cast(Any, self.sm)


def _project_doc(**kw: Any) -> dict[str, Any]:
    base = AnnotationProject(tenant="acme", slug="charters", state=ProjectState.LABELING).model_dump(mode="json")
    base.update(kw)
    return base


def _body(task_id: str) -> dict[str, Any]:
    return Task(
        task_id=task_id,
        project_id="p1",
        source=cast(Any, {"kind": "chunks", "keys": [task_id]}),
        media=cast(Any, {"kind": "image", "image_url": f"s3://b/{task_id}.jpg"}),
    ).model_dump(mode="json")


async def _seeded_actor() -> _Actor:
    actor = _Actor()
    await actor.create(_project_doc())
    actor.sm.saves = 0
    return actor


@pytest.mark.asyncio
async def test_send_many_indexes_the_whole_batch_in_one_save() -> None:
    actor = await _seeded_actor()

    result = await actor.send_many({"tasks": [_body(f"t{k}") for k in range(4)]})

    assert [row["task_id"] for row in result["results"]] == ["t0", "t1", "t2", "t3"]
    assert all(row["created"] for row in result["results"])
    assert sorted(json_index(actor)) == ["t0", "t1", "t2", "t3"]
    assert actor.sm.saves == 1, f"the batch cost {actor.sm.saves} state transactions"
    assert result["counts"][TaskState.UNASSIGNED] == 4


@pytest.mark.asyncio
async def test_send_many_is_idempotent_per_task_id() -> None:
    """`created: False` for an id already indexed is what makes a re-send REPAIR a half-completed
    send rather than reset a task somebody is holding."""
    actor = await _seeded_actor()
    await actor.send_many({"tasks": [_body("t0")]})
    await actor.task_state_changed({"task_id": "t0", "state": str(TaskState.CLAIMED)})

    result = await actor.send_many({"tasks": [_body("t0"), _body("t1")]})

    assert result["results"] == [{"task_id": "t0", "created": False}, {"task_id": "t1", "created": True}]
    assert json_index(actor)["t0"] == str(TaskState.CLAIMED), "a re-send reset a task somebody is holding"


@pytest.mark.asyncio
async def test_send_many_lifts_the_tombstone_of_a_re_sent_task() -> None:
    """A re-sent id that was DROPPED is a deliberate re-add. Leaving the tombstone would make the new
    task live in the index and permanently unable to report where it landed."""
    actor = await _seeded_actor()
    await actor.send_many({"tasks": [_body("t0"), _body("t1")]})
    await actor.drop_task({"task_id": "t0"})

    await actor.send_many({"tasks": [_body("t0")]})

    assert json_index(actor)["t0"] == str(TaskState.UNASSIGNED)
    assert "t0" not in set(__import__("json").loads(actor.sm.store[DROPPED_KEY]))


@pytest.mark.asyncio
async def test_send_many_refuses_a_frozen_project_once_and_writes_nothing() -> None:
    actor = await _seeded_actor()
    await actor.fire({"event": "freeze"})
    actor.sm.saves = 0

    with pytest.raises(IllegalTransition):
        await actor.send_many({"tasks": [_body("t0"), _body("t1")]})

    assert actor.sm.saves == 0
    assert json_index(actor) == {}


def json_index(actor: _Actor) -> dict[str, str]:
    import json  # noqa: PLC0415 - only the assertions need it

    raw = actor.sm.store.get(INDEX_KEY)
    return dict(json.loads(raw)) if raw else {}
