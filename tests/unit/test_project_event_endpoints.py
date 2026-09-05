"""Project lifecycle endpoints — and the two-door publish crossing of §6.2.

The publish doors are the design's most important authz consequence, so most of this file is one
question asked several ways: can anyone move labels into the lakehouse while holding only half the
authority? `can_publish` alone must not be enough, and `can_create_table` alone must not be enough.
Collapsing the two would let either plane's admin quietly acquire the other's.

The actors are faked, so these prove the HTTP contract without a sidecar.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import project_events as ev
from annotator.projects.machines import IllegalTransition
from annotator.projects.models import ProjectState
from service_kit.exceptions import register_handlers
from service_kit.media.deps import get_state


SUBJECT = "henry"


#: The fields `Task` requires and these doubles used to omit. The details fan-out publishes the
#: `Task` model (ANN-07), so a document missing them describes a task the actor cannot store.
_TASK_REQUIRED: dict[str, Any] = {
    "source": {"kind": "chunks", "keys": ["t1"]},
    "media": {"kind": "image", "image_url": "s3://bucket/t1.jpg"},
}


class _FakeProject:
    def __init__(self, state: ProjectState | None = ProjectState.FROZEN) -> None:
        self.state = state
        self.fired: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.raise_on_fire: Exception | None = None
        self.tasks: dict[str, str] = {}

    async def get(self) -> dict[str, Any] | None:
        return None if self.state is None else {"state": str(self.state), "project_id": "p1", "review_required": True, "lease_seconds": 900}

    async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.raise_on_fire:
            raise self.raise_on_fire
        self.fired.append(payload)
        # As the ACTOR answers: `tenant` and `slug` are required on `AnnotationProject`, and the
        # route publishes that model now (docs/DECISIONS.md "The Python estate audit" ANN-07), so a document the actor could
        # not have stored no longer passes through the route either.
        return {"state": "publishing", "project_id": "p1", "tenant": "acme", "slug": "charters"}

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(payload)
        return {"task_id": payload["task_id"], "created": True, "counts": {}}

    async def send_many(self, payload: dict[str, Any]) -> dict[str, Any]:
        # ANN-03 batched the per-task Send door into SendMany (one save_state for the whole send).
        # `extend` keeps `project.sent` populated on the success path — so the `sent[0]` identity
        # assertion still holds — while the atomic foreign-task refusal never reaches here, so
        # `sent == []` on that path stays a real check of the refusal, not a vacuous one.
        self.sent.extend(payload["tasks"])
        return {"results": [{"task_id": t["task_id"], "created": True} for t in payload["tasks"]], "counts": {}}

    async def list_tasks(self) -> dict[str, Any]:
        return {"tasks": dict(self.tasks), "counts": {}, "total": len(self.tasks), "terminal": 0, "may_publish": True}


class _FakeTask:
    """The task actor. `seed` is idempotent: a task that already exists comes back with ITS project."""

    def __init__(self, owner: str | None = None) -> None:  # noqa: D107 - see class docstring
        self.seeded: list[dict[str, Any]] = []
        #: The project the task ALREADY belongs to, when it exists. `seed` is idempotent and returns
        #: what is there, which is how a cross-project send is detectable at all.
        self.owner = owner

    async def seed(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.seeded.append(payload)
        return {**payload, "project_id": self.owner or payload["project_id"]}

    async def get(self) -> dict[str, Any] | None:
        """The details fan-out reads each task's own actor. `docs` is set by the tests that use it;
        None (the default) models a task the index knows and the actor does not."""
        return getattr(self, "docs", {}).get("current")


def _app(project: _FakeProject, *, grant: set[str], seen: list[dict[str, Any]], task: _FakeTask | None = None) -> FastAPI:
    """`grant` is the set of relations this subject holds — everything else is denied."""

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        seen.append({"user": user, "relation": relation, "obj": obj})
        return relation in grant

    app = FastAPI()
    register_handlers(app)
    app.include_router(ev.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: SUBJECT
    # `send` now consults the dataset registry to refuse an item whose media dataset does not
    # resolve, so this router carries `StateDep`. Real app state is built by the service lifespan
    # and does not exist here; this stands in for it. `list_ids` answers the "known datasets are …"
    # half of a refusal, and these tests never exercise a refusal.
    app.dependency_overrides[get_state] = lambda: SimpleNamespace(registry=SimpleNamespace(list_ids=list))
    return app


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch both proxies once; each test picks the grant set and, where it matters, the state.

    The default is FROZEN because most of this file is about publish. `send` is only legal in
    draft/labeling, so those tests pass `state=` explicitly — which is itself the contract.
    """
    holder: dict[str, Any] = {}

    def build(grant: set[str], state: ProjectState = ProjectState.FROZEN) -> tuple[TestClient, _FakeProject, _FakeTask, list[dict[str, Any]]]:
        project, task = _FakeProject(state=state), _FakeTask()
        holder["project"], holder["task"] = project, task
        monkeypatch.setattr(ev, "_project_proxy", lambda _p: project)
        monkeypatch.setattr(ev, "_task_proxy", lambda _t: task)
        seen: list[dict[str, Any]] = []
        return TestClient(_app(project, grant=grant, seen=seen, task=task)), project, task, seen

    return build


ALL_PUBLISH_DOORS = {"can_publish", "can_create_table", "can_promote"}


# --------------------------------------------------------------------------------------------------
# The publish doors — §6.2
# --------------------------------------------------------------------------------------------------


def test_publish_needs_every_door(wired: Any) -> None:
    client, project, _t, seen = wired(ALL_PUBLISH_DOORS)

    r = client.post("/projects/p1/events", json={"event": "publish"})

    assert r.status_code == 200, r.text
    relations = [c["relation"] for c in seen]
    assert relations == ["can_publish", "can_create_table", "can_promote"], "the doors must be crossed in order"
    assert seen[0]["obj"] == "annotation_project:p1", "door 1 is the annotator's own domain"
    assert seen[1]["obj"] == "namespace:silver", "door 2 is the TARGET NAMESPACE, not the project"
    assert project.fired, "the transition never reached the actor"


@pytest.mark.parametrize(
    ("held", "closed"),
    [
        ({"can_create_table", "can_promote"}, "can_publish"),
        ({"can_publish", "can_promote"}, "can_create_table"),
        ({"can_publish", "can_create_table"}, "can_promote"),
    ],
)
def test_holding_only_some_doors_is_403(wired: Any, held: set[str], closed: str) -> None:
    """Half the authority is not authority. Each door is load-bearing on its own."""
    client, project, _t, _seen = wired(held)

    r = client.post("/projects/p1/events", json={"event": "publish"})

    assert r.status_code == 403
    assert closed in r.text, "the refusal must name the door that closed"
    assert project.fired == [], "the publish reached the actor despite a closed door"


def test_the_audit_names_the_first_door_that_closed_not_a_composite(wired: Any) -> None:
    """Short-circuiting is deliberate: "publish denied" is not actionable, "lacks can_create_table on
    namespace:silver" is."""
    client, _p, _t, seen = wired(set())

    r = client.post("/projects/p1/events", json={"event": "publish"})

    assert r.status_code == 403
    assert len(seen) == 1, "later doors were checked after the first one closed"
    assert "can_publish" in r.text


def test_an_ungated_namespace_skips_the_promote_door(wired: Any) -> None:
    """Door 3 is conditional — only a validator-gated medallion stage. A publish into a plain
    namespace must not demand a rung that namespace does not have."""
    client, _p, _t, seen = wired({"can_publish", "can_create_table"})

    r = client.post("/projects/p1/events", json={"event": "publish", "tenant": "acme", "target_namespace": "scratch"})

    assert r.status_code == 200, r.text
    assert [c["relation"] for c in seen] == ["can_publish", "can_create_table"]
    assert seen[1]["obj"] == "namespace:scratch", "the doors are checked wherever the publish points"


def test_the_default_target_is_silver(wired: Any) -> None:
    """Human labels are curated, not raw (§6.2)."""
    client, _p, _t, seen = wired(ALL_PUBLISH_DOORS)
    client.post("/projects/p1/events", json={"event": "publish"})
    assert seen[1]["obj"] == "namespace:silver"


def test_an_actor_precondition_failure_is_409_not_500(wired: Any) -> None:
    """Every-task-terminal lives in the ACTOR, so it holds for any caller — including a workflow
    retrying after a crash. The endpoint must surface it as a conflict, not an error."""
    client, project, _t, _seen = wired(ALL_PUBLISH_DOORS)
    project.raise_on_fire = IllegalTransition("project", "frozen", "publish (tasks are not all terminal)")

    r = client.post("/projects/p1/events", json={"event": "publish"})

    assert r.status_code == 409
    assert "not all terminal" in r.text


# --------------------------------------------------------------------------------------------------
# The other edges read their permission from the machine
# --------------------------------------------------------------------------------------------------


def test_a_non_publish_edge_checks_the_permission_the_table_declares(wired: Any) -> None:
    client, _p, _t, seen = wired({"can_manage"})
    r = client.post("/projects/p1/events", json={"event": "open"})
    assert r.status_code == 200, r.text
    assert seen == [{"user": SUBJECT, "relation": "can_manage", "obj": "annotation_project:p1"}]


def test_an_edge_absent_from_the_table_is_409(wired: Any) -> None:
    client, _p, _t, _seen = wired({"can_manage"})
    r = client.post("/projects/p1/events", json={"event": "archive"})
    assert r.status_code == 200, r.text  # frozen -> archived IS legal
    r2 = client.post("/projects/p1/events", json={"event": "freeze"})
    assert r2.status_code == 409, "archived -> freeze is not in the table"


@pytest.mark.parametrize("event", ["publish_succeeded", "publish_failed"])
def test_a_system_only_project_edge_is_refused_over_http(wired: Any, event: str) -> None:
    """These are fired by the publish saga. Exposed unauthenticated — which is what "permission is
    None" produced before — anyone could mark another operator\'s publish succeeded, or fail one
    that was mid-flight."""
    client, project, _t, _seen = wired({"can_manage", "can_publish", "can_create_table", "can_promote"})

    r = client.post("/projects/p1/events", json={"event": event})

    assert r.status_code == 403
    assert project.fired == [], "a system-only edge reached the actor over HTTP"


def test_an_absent_project_is_409_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _FakeProject(state=None)
    monkeypatch.setattr(ev, "_project_proxy", lambda _p: project)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(project, grant=ALL_PUBLISH_DOORS, seen=seen))
    assert client.post("/projects/p1/events", json={"event": "publish"}).status_code == 409


# --------------------------------------------------------------------------------------------------
# Send
# --------------------------------------------------------------------------------------------------


def _item(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "source": {"kind": "chunks", "keys": [task_id]},
        "media": {"kind": "image", "image_url": f"s3://b/{task_id}.jpg"},
    }


def test_send_seeds_the_task_actor_before_indexing_it(wired: Any) -> None:
    """The order is the correctness argument. A crash between the two leaves a task that exists but
    is not indexed — invisible to the publish precondition, and repaired by an idempotent re-send.
    The reverse would index a task whose actor was never seeded, so the precondition would read a
    state for a task that cannot answer for itself."""
    client, project, task, _seen = wired({"can_send_items"}, state=ProjectState.LABELING)

    r = client.post("/projects/p1/items", json={"items": [_item("t0")]})

    assert r.status_code == 201, r.text
    assert task.seeded, "the task actor was never seeded"
    assert project.sent, "the task was never indexed"
    assert task.seeded[0]["task_id"] == project.sent[0]["task_id"]


def test_send_forces_the_project_id_from_the_path(wired: Any) -> None:
    """A client-supplied `project_id` in the body must not be able to file a task under a project the
    caller was not authorized against."""
    client, _p, task, _seen = wired({"can_send_items"}, state=ProjectState.LABELING)
    client.post("/projects/p1/items", json={"items": [_item("t0")]})
    assert task.seeded[0]["project_id"] == "p1"


def test_send_without_the_permission_writes_nothing(wired: Any) -> None:
    client, project, task, _seen = wired(set(), state=ProjectState.LABELING)
    r = client.post("/projects/p1/items", json={"items": [_item("t0")]})
    assert r.status_code == 403
    assert task.seeded == [] and project.sent == []


def test_listing_tasks_returns_the_precondition_from_the_same_snapshot(wired: Any) -> None:
    client, _p, _t, _seen = wired({"can_view"})
    body = client.get("/projects/p1/tasks", params={}).json()
    assert "may_publish" in body and "counts" in body, "the caller must not have to ask twice"


# --------------------------------------------------------------------------------------------------
# Send: the client describes WHAT to annotate, never who did what
# --------------------------------------------------------------------------------------------------


def test_a_sender_cannot_fabricate_state_or_provenance(wired: Any) -> None:
    """The defect this replaces: `items` was a full `Task`, so a sender could supply
    `state=accepted`, `submitted_by` and `reviewed_by` — manufacturing reviewed work with forged
    provenance and walking it straight into a publish. The request model now accepts only `source`
    and `media`; every provenance field takes its server-side default."""
    client, _p, task, _seen = wired({"can_send_items"}, state=ProjectState.LABELING)

    r = client.post(
        "/projects/p1/items",
        json={
            "items": [
                {
                    **_item("t0"),
                    "state": "accepted",
                    "submitted_by": "mallory",
                    "reviewed_by": "mallory",
                    "review_action": "accepted",
                }
            ]
        },
    )

    assert r.status_code == 201, r.text
    seeded = task.seeded[0]
    assert seeded["state"] == "unassigned", "a sender dictated the task state"
    assert seeded["submitted_by"] is None and seeded["reviewed_by"] is None, "a sender forged provenance"


def test_send_captures_review_required_from_the_project(wired: Any) -> None:
    """It has to be captured at send time: the task actor decides `submit`\'s target from it, and
    reading it per-request would put it back in the caller\'s hands."""
    client, _p, task, _seen = wired({"can_send_items"}, state=ProjectState.LABELING)
    client.post("/projects/p1/items", json={"items": [_item("t0")]})
    assert task.seeded[0]["review_required"] is True


def test_a_task_owned_by_another_project_is_refused_not_indexed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`task_id` is client-supplied. Re-sending one that already belongs to p1 into p2 used to write
    p2's index entry from the PAYLOAD — but the task's own `_report_state` only ever addresses its
    real owner, so p2's entry froze at the seeded value. Non-terminal, `may_publish` false forever,
    no remove-item endpoint: one malformed send permanently stranded every other label in p2."""
    project, task = _FakeProject(state=ProjectState.LABELING), _FakeTask(owner="some-other-project")
    monkeypatch.setattr(ev, "_project_proxy", lambda _p: project)
    monkeypatch.setattr(ev, "_task_proxy", lambda _t: task)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(project, grant={"can_send_items"}, seen=seen, task=task))

    r = client.post("/projects/p1/items", json={"items": [_item("t0")]})

    assert r.status_code == 409, r.text
    assert "already belongs to project" in r.text
    assert project.sent == [], "a foreign task was written into this project's index"


# --------------------------------------------------------------------------------------------------
# The details listing — what A2's queue actually renders
# --------------------------------------------------------------------------------------------------


def test_details_listing_fans_out_and_carries_legal_events(wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The index is task_id→state by design; the queue needs assignee/lease/media and the ACTIONS.
    `legal_events` per task comes from the machine tables — A2/A3 render what the backend supplies."""
    client, project, _t, _seen = wired({"can_view"})
    project.tasks = {"t1": "claimed", "t2": "in_review", "ghost": "claimed"}
    docs: dict[str, dict[str, Any]] = {
        "t1": {**_TASK_REQUIRED, "task_id": "t1", "project_id": "p1", "state": "claimed", "assignee": "gina", "lease_expires_at": "2026-07-31T12:00:00+00:00"},
        "t2": {**_TASK_REQUIRED, "task_id": "t2", "project_id": "p1", "state": "in_review", "assignee": None, "submitted_by": "gina"},
    }

    class _PerTask:
        def __init__(self, task_id: str) -> None:
            self.task_id = task_id

        async def get(self) -> dict[str, Any] | None:
            return docs.get(self.task_id)

    monkeypatch.setattr(ev, "_task_proxy", _PerTask)

    r = client.get("/projects/p1/tasks", params={"include": "details"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert [d["task_id"] for d in body["details"]] == ["t1", "t2"]
    assert body["missing"] == ["ghost"], "a task the index knows but no actor holds must be NAMED, not dropped"
    by_id = {d["task_id"]: d for d in body["details"]}
    assert {e["event"] for e in by_id["t1"]["legal_events"]} == {"save_draft", "submit", "release", "skip"}
    assert {e["event"] for e in by_id["t2"]["legal_events"]} == {"accept", "fix_and_accept", "request_changes"}
    assert "lease_expired" not in {e["event"] for d in body["details"] for e in d["legal_events"]}


def test_the_plain_listing_is_unchanged_by_the_details_option(wired: Any) -> None:
    client, project, _t, _seen = wired({"can_view"})
    project.tasks = {"t1": "claimed"}
    r = client.get("/projects/p1/tasks")
    assert r.status_code == 200
    assert "details" not in r.json()


# --------------------------------------------------------------------------------------------------
# Send captures the project's lease
# --------------------------------------------------------------------------------------------------


def test_send_captures_the_projects_lease_seconds_onto_the_task(wired: Any) -> None:
    """Like `review_required`: project config the claim path reads off the TASK. Without the
    capture, the project's lease setting is stored and never read."""
    client, _p, task, _seen = wired({"can_send_items"}, state=ProjectState.LABELING)

    r = client.post(
        "/projects/p1/items",
        json={"items": [{"source": {"kind": "chunks", "keys": ["k1"]}, "media": {"kind": "image", "image_url": "s3://b/x.jpg"}}]},
    )

    assert r.status_code == 201, r.text
    assert task.seeded[0]["lease_seconds"] == 900, "the task did not capture the project's lease"


def test_details_on_a_frozen_project_carry_no_task_events(wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 5: nothing escapes a published project. The tasks' own states still admit edges
    (accepted → reopen), but the PROJECT gate refuses them all — so the listing must not hand the
    UI actions that can only 409. Found by LOOKING at the published screenshot: Reopen buttons on
    a published project."""
    client, project, _t, _seen = wired({"can_view"}, state=ProjectState.PUBLISHED)
    project.tasks = {"t1": "accepted"}
    docs = {"t1": {**_TASK_REQUIRED, "task_id": "t1", "project_id": "p1", "state": "accepted"}}

    class _PerTask:
        def __init__(self, task_id: str) -> None:
            self.task_id = task_id

        async def get(self) -> dict[str, Any] | None:
            return docs.get(self.task_id)

    monkeypatch.setattr(ev, "_task_proxy", _PerTask)

    r = client.get("/projects/p1/tasks", params={"include": "details"})

    assert r.status_code == 200, r.text
    assert r.json()["details"][0]["legal_events"] == [], "a published project handed the UI a Reopen that can only 409"


# --------------------------------------------------------------------------------------------------
# Consensus v1 — send seeds N independent replicas per item
# --------------------------------------------------------------------------------------------------


def test_send_with_consensus_seeds_n_replicas_per_item(wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """consensus_n=3 → each sent item becomes THREE independent items sharing one replica group,
    with deterministic sibling ids (`{gid}-r{k}`) — determinism is what lets the one-replica-per-
    annotator guard find the siblings without an index."""
    client, _project, task, _seen = wired({"can_send_items"}, state=ProjectState.LABELING)
    monkeypatch.setattr(
        _FakeProject,
        "get",
        lambda self: _consensus_get(self),
    )

    r = client.post(
        "/projects/p1/items",
        json={"items": [{"task_id": "item1", "source": {"kind": "chunks", "keys": ["k1"]}, "media": {"kind": "image"}}]},
    )

    assert r.status_code == 201, r.text
    seeded = task.seeded
    assert [s["task_id"] for s in seeded] == ["item1-r1", "item1-r2", "item1-r3"]
    assert all(s["replica_of"] == "item1" for s in seeded)
    assert {s["source"]["keys"][0] for s in seeded} == {"k1"}, "replicas must share the source"
    assert r.json()["sent"] == 1 and r.json()["created"] == 3


def test_send_refuses_when_items_times_consensus_would_exceed_the_cap(wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """334 items × consensus_n=3 = 1002 tasks — over the 1000-task cap the request schema enforces
    for plain sends. Replica expansion must not become a backdoor around it."""
    client, _project, task, _seen = wired({"can_send_items"}, state=ProjectState.LABELING)
    monkeypatch.setattr(_FakeProject, "get", lambda self: _consensus_get(self))

    items = [{"source": {"kind": "chunks", "keys": [f"k{i}"]}, "media": {"kind": "image"}} for i in range(334)]
    r = client.post("/projects/p1/items", json={"items": items})

    assert r.status_code == 409, r.text
    assert "exceeds the 1000-task send cap" in r.json()["detail"]
    assert task.seeded == [], "a refused send must seed NOTHING"


async def _consensus_get(self: Any) -> dict[str, Any] | None:
    if self.state is None:
        return None
    return {"state": str(self.state), "project_id": "p1", "review_required": True, "lease_seconds": 900, "consensus_n": 3}


# --------------------------------------------------------------------------------------------------
# Consensus v1 — the adjudication endpoint (manager-gated pick)
# --------------------------------------------------------------------------------------------------


def test_adjudicate_is_gated_on_can_manage_and_reaches_the_actor(wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adjudication decides which OPINION wins — the manager's authority, not a review action."""
    client, _project, _task, seen = wired({"can_manage"}, state=ProjectState.LABELING)
    picks: list[dict[str, Any]] = []

    async def _adjudicate(self: Any, payload: dict[str, Any]) -> dict[str, Any]:
        picks.append(payload)
        # A whole `AnnotationProject`, as the actor answers — `tenant`/`slug` are required, and
        # `Adjudication` carries `by`/`at` beside the pick.
        return {
            "project_id": "p1",
            "tenant": "acme",
            "slug": "charters",
            "adjudications": {payload["group"]: {"task_id": payload["task_id"], "by": payload["actor"], "at": "2026-07-31T12:00:00+00:00"}},
        }

    monkeypatch.setattr(_FakeProject, "adjudicate", _adjudicate, raising=False)

    r = client.put("/projects/p1/adjudications/g1", json={"task_id": "g1-r2"})

    assert r.status_code == 200, r.text
    assert picks == [{"group": "g1", "task_id": "g1-r2", "actor": SUBJECT}]
    assert ("can_manage", "annotation_project:p1") in [(c["relation"], c["obj"]) for c in seen]


def test_adjudicate_without_can_manage_is_403(wired: Any) -> None:
    client, _project, _task, _seen = wired({"can_review"}, state=ProjectState.LABELING)

    r = client.put("/projects/p1/adjudications/g1", json={"task_id": "g1-r1"})

    assert r.status_code == 403, r.text
    assert "can_manage" in r.json()["detail"]


def test_adjudicate_surfaces_the_actors_refusal_as_409(wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from annotator.projects.machines import IllegalTransition

    client, _project, _task, _seen = wired({"can_manage"}, state=ProjectState.LABELING)

    async def _refuse(self: Any, payload: dict[str, Any]) -> dict[str, Any]:
        raise IllegalTransition("project", "labeling", "adjudicate (g1-r1 is skipped, not accepted)")

    monkeypatch.setattr(_FakeProject, "adjudicate", _refuse, raising=False)

    r = client.put("/projects/p1/adjudications/g1", json={"task_id": "g1-r1"})

    assert r.status_code == 409, r.text
    assert "not accepted" in r.json()["detail"]


def test_clearing_an_adjudication_is_a_manager_gated_delete(wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _project, _task, seen = wired({"can_manage"}, state=ProjectState.LABELING)
    picks: list[dict[str, Any]] = []

    async def _adjudicate(self: Any, payload: dict[str, Any]) -> dict[str, Any]:
        picks.append(payload)
        return {"project_id": "p1", "tenant": "acme", "slug": "charters", "adjudications": {}}

    monkeypatch.setattr(_FakeProject, "adjudicate", _adjudicate, raising=False)

    r = client.delete("/projects/p1/adjudications/g1")

    assert r.status_code == 200, r.text
    assert picks == [{"group": "g1", "task_id": None, "actor": SUBJECT}]
    assert ("can_manage", "annotation_project:p1") in [(c["relation"], c["obj"]) for c in seen]


def test_send_captures_the_projects_ontology_onto_every_item(wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ontology rides each item like review_required/lease_seconds — enforcement reads the
    ITEM's copy, so a mid-flight ontology edit cannot retroactively invalidate work in review.

    Capturing the TAXONOMY is also what makes the closed-set label check possible at all: the send
    used to copy the `template` and leave `label_schema` behind on the project, so the class list
    was not in scope where enforcement happens and `label="asdf"` submitted and published."""
    client, _project, task, _seen = wired({"can_send_items"}, state=ProjectState.LABELING)

    async def _ontology_get(self: Any) -> dict[str, Any] | None:
        return {
            "state": str(self.state),
            "project_id": "p1",
            "review_required": True,
            "lease_seconds": 900,
            "ontology": {
                "kind": "document-question-answering",
                "classes": [{"name": "question", "tools": ["bbox"]}, {"name": "answer", "tools": ["bbox"]}],
            },
        }

    monkeypatch.setattr(_FakeProject, "get", _ontology_get)

    r = client.post(
        "/projects/p1/items",
        json={"items": [{"source": {"kind": "chunks", "keys": ["k1"]}, "media": {"kind": "image"}}]},
    )

    assert r.status_code == 201, r.text
    assert task.seeded[0]["ontology"]["kind"] == "document-question-answering"
    # The TAXONOMY rides too — not just the enforcement knobs. That is the half that was missing.
    assert [c["name"] for c in task.seeded[0]["ontology"]["classes"]] == ["question", "answer"]
