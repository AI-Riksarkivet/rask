"""Task endpoints — the authorization surface of the CVAT/Label-Studio loop.

The actor is faked so these prove the HTTP contract without a sidecar: which `can_*` each event
demands, on WHICH object, and the rules the transition table cannot express because they depend on
the task's own rows.

The permission is not asserted per-route by hand. `TASK_EDGES` carries it on the edge, so these read
the expectation from the machine — a new edge is gated automatically and a route cannot quietly drift
from the model.

Three of these exist because an adversarial review found the corresponding defect in the first
version of this file's subject, and the ORIGINAL tests asserted the buggy behaviour as correct:

- the check ran on `project:<tenant>`, a type on which `model.fga` defines none of these relations;
- `review_required` was a request field, so an annotator could submit with review waived;
- system-only edges were exposed unauthenticated, because "no permission required" was read as
  "no permission checked".
"""

from __future__ import annotations

import struct
from typing import Any

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import tasks as tasks_ep
from annotator.projects.machines import TASK_EDGES
from annotator.projects.models import ProjectState, TaskState
from service_kit.exceptions import register_handlers


SUBJECT = "gina"
PROJECT_ID = "proj-1"


class _FakeActor:
    """Stands in for the Dapr actor proxy: records what it was asked, returns canned state."""

    def __init__(self, state: dict[str, Any] | None, draft: dict[str, Any] | None = None) -> None:
        self.state = state
        self.fired: list[dict[str, Any]] = []
        self.drafts: list[dict[str, Any]] = []
        #: What `get_draft` answers. Overridable so the import tests can put existing work in the
        #: draft — appending onto an EMPTY draft would prove nothing about not destroying anything.
        self.draft: dict[str, Any] = draft if draft is not None else {"revision": 1, "shapes": []}

    async def get(self) -> dict[str, Any] | None:
        return self.state

    async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.fired.append(payload)
        return {**(self.state or {}), "state": "claimed"}

    async def save_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.drafts.append(payload)
        # A `Draft` as the actor stores one — `task_id`/`project_id`/`author` are required on the
        # model and were missing here, so this double answered a document the actor cannot produce.
        return {
            "revision": 1,
            "shapes": payload.get("shapes", []),
            "links": payload.get("links", []),
            "task_id": str(payload.get("task_id", "t1")),
            "project_id": str(payload.get("project_id", PROJECT_ID)),
            "author": str(payload.get("author", SUBJECT)),
            "origin": payload.get("origin", "human"),
        }

    async def get_draft(self) -> dict[str, Any] | None:
        return self.draft


def _app(*, allow: bool, seen: list[dict[str, Any]] | None = None) -> FastAPI:
    async def checker(*, user: str, relation: str, obj: str) -> bool:
        if seen is not None:
            seen.append({"user": user, "relation": relation, "obj": obj})
        return allow

    app = FastAPI()
    register_handlers(app)
    app.include_router(tasks_ep.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: SUBJECT
    return app


def _task(**kw: Any) -> dict[str, Any]:
    """A task document as the ACTOR produces one.

    `source` and `media` are required on `Task` and were absent here: the double described a
    document the actor cannot store, which is exactly the shape of double that lets a route break
    with the suite green (`test_publish_token_after_credential_removal.py` has the same lesson).
    The routes now publish the `Task` model (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" ANN-07), so a document that could
    not exist no longer passes through them either.
    """
    base: dict[str, Any] = {
        "state": TaskState.UNASSIGNED,
        "assignee": None,
        "submitted_by": None,
        "task_id": "t1",
        "project_id": PROJECT_ID,
        "source": {"kind": "chunks", "keys": ["t1"]},
        "media": {"kind": "image", "image_url": "s3://bucket/t1.jpg"},
    }
    base.update(kw)
    return base


class _FakeProjectActor:
    """The project the task belongs to. Read on every task event to enforce §5.2 rule 5."""

    def __init__(self, state: ProjectState = ProjectState.LABELING) -> None:
        self.state = state
        self.consensus_n = 1

    async def get(self) -> dict[str, Any]:
        return {"state": str(self.state), "project_id": PROJECT_ID, "consensus_n": self.consensus_n}


@pytest.fixture(autouse=True)
def _live_project(monkeypatch: pytest.MonkeyPatch) -> _FakeProjectActor:
    """Every task event now reads its project's state (§5.2 rule 5 — nothing escapes a published
    project). Autouse so each test gets a LABELING project by default; the freeze tests mutate it."""
    from annotator.api.v1.endpoints import project_events as pe

    project = _FakeProjectActor()
    monkeypatch.setattr(pe, "_project_proxy", lambda _p: project)
    return project


# --------------------------------------------------------------------------------------------------
# The permission — and the object — come from the model, not from the route
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("state", "event"), [(TaskState.UNASSIGNED, "claim"), (TaskState.UNASSIGNED, "assign"), (TaskState.CLAIMED, "release")])
def test_every_edge_checks_the_permission_the_table_declares(state: TaskState, event: str, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = TASK_EDGES[(state, event)][1]
    actor = _FakeActor(_task(state=state))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(allow=True, seen=seen))

    r = client.post("/tasks/t1/events", json={"event": event})

    assert r.status_code == 200, r.text
    assert seen, f"{event} from {state} was not authorized at all"
    assert seen[0]["relation"] == expected
    assert seen[0]["user"] == SUBJECT, "the check must use the VERIFIED subject"


# --------------------------------------------------------------------------------------------------
# System-only edges are not a public surface
# --------------------------------------------------------------------------------------------------


def test_a_system_only_edge_is_refused_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """`lease_expired` has permission `None` because no PRINCIPAL fires it — the actor's reminder
    does. Treating "no permission required" as "no permission checked" exposed it unauthenticated,
    letting anyone strip another annotator's claim. It is refused outright."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(allow=True, seen=seen))

    r = client.post("/tasks/t1/events", json={"event": "lease_expired"})

    assert r.status_code == 403
    assert actor.fired == [], "a system-only edge reached the actor over HTTP"


def test_every_permissionless_edge_is_in_the_refused_set() -> None:
    """Derived from the machine, so a new `None`-permission edge is refused without a code change."""
    from annotator.api.v1.endpoints.tasks import SYSTEM_ONLY_EVENTS

    assert {e for (_s, e), (_t, p) in TASK_EDGES.items() if p is None} == SYSTEM_ONLY_EVENTS
    assert "lease_expired" in SYSTEM_ONLY_EVENTS


# --------------------------------------------------------------------------------------------------
# review_required is not a request field
# --------------------------------------------------------------------------------------------------


def test_an_annotator_cannot_waive_review_from_the_request_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect this replaces: `review_required` was a `FireRequest` field, so a submitter could
    pass `false` and land straight in `accepted` — self-approving past the one guarantee the whole
    plane exists to provide. It is captured on the task at send time and ignored here."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/events", json={"event": "submit", "review_required": False})

    assert r.status_code == 200, r.text
    assert "review_required" not in actor.fired[0], "the request body reached the actor's submit decision"


def test_a_denied_check_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task())
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=False))
    r = client.post("/tasks/t1/events", json={"event": "claim"})
    assert r.status_code == 403
    assert actor.fired == [], "the actor was invoked despite the check failing"


def test_an_edge_absent_from_the_table_is_409_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task(state=TaskState.UNASSIGNED))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))
    assert client.post("/tasks/t1/events", json={"event": "accept"}).status_code == 409


# --------------------------------------------------------------------------------------------------
# The rules the table cannot express
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["accept", "fix_and_accept", "request_changes"])
def test_a_reviewer_may_not_review_their_own_submission(event: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """§5.2. `can_review` is not enough — the submitter is excluded even when they hold it."""
    actor = _FakeActor(_task(state=TaskState.IN_REVIEW, submitted_by=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/events", json={"event": event})

    assert r.status_code == 403
    assert "own submission" in r.text
    assert actor.fired == []


def test_someone_elses_submission_can_be_reviewed(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task(state=TaskState.IN_REVIEW, submitted_by="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))
    assert client.post("/tasks/t1/events", json={"event": "accept"}).status_code == 200


# --------------------------------------------------------------------------------------------------
# Drafts
# --------------------------------------------------------------------------------------------------


def test_the_draft_save_records_the_verified_subject_as_author(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provenance: who annotated is the VERIFIED subject, never a field the client supplied."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.put("/tasks/t1/draft", json={"shapes": [{"shape_type": "bbox"}]})

    assert r.status_code == 200, r.text
    assert actor.drafts[0]["author"] == SUBJECT
    assert actor.drafts[0]["project_id"] == PROJECT_ID, "the project id must come from the task, not the body"


@pytest.mark.parametrize("state", [TaskState.ACCEPTED])
def test_a_draft_is_only_writable_while_the_task_is_claimed(state: TaskState, monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect this replaces: `save_draft` consulted the assignee but never the STATE, so an
    ACCEPTED task's shapes could be rewritten after review — and during a publish — putting
    annotations into the lakehouse no reviewer ever saw. `TASK_EDGES` already says `save_draft` is
    legal only from CLAIMED; the route now honours the machine instead of writing around it."""
    actor = _FakeActor(_task(state=state, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.put("/tasks/t1/draft", json={"shapes": [{"shape_type": "bbox"}]})

    assert r.status_code == 409, r.text
    assert actor.drafts == [], f"a draft was written to a {state} task"


def test_a_draft_save_by_a_non_holder_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))
    r = client.put("/tasks/t1/draft", json={"shapes": []})
    assert r.status_code == 403
    assert actor.drafts == []


def test_an_unsent_task_is_409_rather_than_a_500(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(None)
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))
    assert client.post("/tasks/t1/events", json={"event": "claim"}).status_code == 409


def test_reading_a_task_authorizes_before_returning_it(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task())
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(allow=False, seen=seen))

    r = client.get("/tasks/t1")

    assert r.status_code == 403
    assert seen[0] == {"user": SUBJECT, "relation": "can_view", "obj": f"annotation_project:{PROJECT_ID}"}


# --------------------------------------------------------------------------------------------------
# §5.2 rule 5 — nothing escapes a published project
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("frozen", [ProjectState.PUBLISHING, ProjectState.PUBLISHED, ProjectState.ARCHIVED])
def test_no_task_transition_survives_a_published_project(frozen: ProjectState, _live_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provenance is frozen with the artifact. A task that moved after the publish would describe a
    dataset that no longer matches it — and during `publishing` it could change the very rows the
    saga is mid-way through reading. The rule was documented and enforced nowhere."""
    _live_project.state = frozen
    actor = _FakeActor(_task(state=TaskState.IN_REVIEW, submitted_by="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/events", json={"event": "accept"})

    assert r.status_code == 409, r.text
    assert "frozen with the published artifact" in r.text
    assert actor.fired == []


def test_a_draft_cannot_be_saved_into_a_publishing_project(_live_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sharpest case: the saga is reading drafts right now to build the plan."""
    _live_project.state = ProjectState.PUBLISHING
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.put("/tasks/t1/draft", json={"shapes": [{"shape_type": "bbox"}]})

    assert r.status_code == 409
    assert actor.drafts == []


# --------------------------------------------------------------------------------------------------
# §5.2 rule 2 — only the lease holder writes, "even a manager"
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["submit", "skip"])
def test_skip_and_submit_are_lease_holder_only(event: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`skip` was missing from this check, so any annotator could discard work someone else was
    holding — and with `requeue_for_others`, consume their turn at it."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/events", json={"event": event})

    assert r.status_code == 403
    assert "held by dave" in r.text
    assert actor.fired == []


def test_release_is_refused_to_a_non_holder_without_can_manage(monkeypatch: pytest.MonkeyPatch) -> None:
    """§5.2: `release` is "lease holder OR can_manage". Gating it on plain `can_annotate` let any
    annotator break another's claim."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    seen: list[dict[str, Any]] = []

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        seen.append({"relation": relation})
        return relation == "can_annotate"  # holds the rung, but is not a manager

    app = FastAPI()
    register_handlers(app)
    app.include_router(tasks_ep.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: SUBJECT
    r = TestClient(app).post("/tasks/t1/events", json={"event": "release"})

    assert r.status_code == 403
    assert "needs can_manage" in r.text
    assert {"relation": "can_manage"} in seen, "the manager escape hatch was never consulted"


def test_a_manager_may_release_a_task_held_by_someone_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented escape hatch for a task pinned to someone unavailable."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))  # holds everything, including can_manage

    r = client.post("/tasks/t1/events", json={"event": "release"})

    assert r.status_code == 200, r.text
    assert actor.fired[0]["event"] == "release"


# --------------------------------------------------------------------------------------------------
# assign names a recipient
# --------------------------------------------------------------------------------------------------


def test_a_manager_assigns_to_a_NAMED_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this replaces made `assign` set the assignee to the manager who fired it, so a manager
    could only ever assign to themselves — silently turning the one manager-driven distribution
    mechanism in the whole plane into a self-claim."""
    actor = _FakeActor(_task(state=TaskState.UNASSIGNED))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/events", json={"event": "assign", "assignee": "dave"})

    assert r.status_code == 200, r.text
    assert actor.fired[0]["assignee"] == "dave"
    assert actor.fired[0]["actor"] == SUBJECT, "the manager is still recorded as the actor"


# --------------------------------------------------------------------------------------------------
# Consensus v1 — one replica per annotator per group
# --------------------------------------------------------------------------------------------------


def test_claiming_a_second_replica_of_the_same_group_is_409(monkeypatch: pytest.MonkeyPatch, _live_project: _FakeProjectActor) -> None:
    """Independence is the point of consensus: the same person labeling two replicas of one item is
    one opinion counted twice. Enforced server-side at claim — sibling ids are deterministic
    (`{gid}-r{k}`), so the guard reads the siblings' own actors and refuses with the rule NAMED."""
    _live_project.consensus_n = 2
    docs = {
        "g1-r1": _task(task_id="g1-r1", state=TaskState.CLAIMED, assignee=SUBJECT, replica_of="g1"),
        "g1-r2": _task(task_id="g1-r2", replica_of="g1"),
    }

    class _PerTask(_FakeActor):
        def __init__(self, task_id: str) -> None:
            super().__init__(docs.get(task_id))

        async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("the guard must refuse BEFORE the actor fires")

    monkeypatch.setattr(tasks_ep, "_proxy", _PerTask)

    client = TestClient(_app(allow=True))
    r = client.post("/tasks/g1-r2/events", json={"event": "claim"})

    assert r.status_code == 409, r.text
    assert "one replica per annotator" in r.json()["detail"]
    assert "g1-r1" in r.json()["detail"], "the refusal must name the replica already held"


def test_assigning_a_second_replica_to_the_same_recipient_is_409(monkeypatch: pytest.MonkeyPatch, _live_project: _FakeProjectActor) -> None:
    """The assign edge is guarded on the RECIPIENT (payload.assignee), not the manager firing it —
    a manager distributing two replicas of one item to the same annotator defeats independence
    exactly like a double claim. The sibling here was already WORKED (submitted_by) with the lease
    long released, which is precisely the state a naive assignee-only check would miss."""
    _live_project.consensus_n = 2
    docs = {
        "g1-r1": _task(task_id="g1-r1", state=TaskState.IN_REVIEW, submitted_by="dave", replica_of="g1"),
        "g1-r2": _task(task_id="g1-r2", replica_of="g1"),
    }

    class _PerTask(_FakeActor):
        def __init__(self, task_id: str) -> None:
            super().__init__(docs.get(task_id))

        async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("the guard must refuse BEFORE the actor fires")

    monkeypatch.setattr(tasks_ep, "_proxy", _PerTask)

    client = TestClient(_app(allow=True))
    r = client.post("/tasks/g1-r2/events", json={"event": "assign", "assignee": "dave"})

    assert r.status_code == 409, r.text
    assert "one replica per annotator" in r.json()["detail"]
    assert "g1-r1" in r.json()["detail"], "the refusal must name the replica already worked"


# --------------------------------------------------------------------------------------------------
# Import (#39) — Arrow IPC into the draft
# --------------------------------------------------------------------------------------------------


def _ipc(rows: list[dict[str, Any]]) -> bytes:
    import io

    import pyarrow as pa

    table = pa.Table.from_pylist(rows)
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def test_an_import_APPENDS_to_the_draft_rather_than_replacing_it(_live_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The design decision, pinned.

    A whole-draft replace matches how `save_draft` STORES — one keyed write — and was rejected: an
    import onto a task somebody had already worked would silently destroy that work, and there is no
    undo anywhere in the actor. Appending is never destructive; a duplicate is removable on the
    canvas, which is a recoverable annoyance instead of an unrecoverable loss.
    """
    actor = _FakeActor(
        _task(state=TaskState.CLAIMED, assignee=SUBJECT),
        draft={"revision": 4, "shapes": [{"shape_id": "drawn-1", "shape_type": "bbox"}], "links": []},
    )
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/import", content=_ipc([{"id": "a1", "shape_type": "bbox", "label": "figure"}]))

    assert r.status_code == 200, r.text
    saved = actor.drafts[0]["shapes"]
    assert [s["shape_id"] for s in saved] == ["drawn-1", "a1"], "the hand-drawn shape was destroyed by the import"
    assert r.json()["imported"] == 1


def test_the_TASKS_OWN_ontology_refuses_a_foreign_label(_live_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rules come from the task, never from the request — a caller must not be able to supply the
    contract it is judged by."""
    actor = _FakeActor(
        _task(
            state=TaskState.CLAIMED,
            assignee=SUBJECT,
            ontology={"kind": "detection", "classes": [{"name": "figure"}], "allow_empty": True},
        ),
        draft={"revision": 1, "shapes": [], "links": []},
    )
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/import", content=_ipc([{"id": "a1", "shape_type": "bbox", "label": "spaceship"}]))

    # 400, not 422: every refusal here — malformed Arrow, a foreign label, a dangling relation — means
    # the same thing to the caller ("fix the file and retry") and each one names its offender in
    # words. Two status codes for one user action would be precision with no consumer.
    assert r.status_code == 400, r.text
    assert "spaceship" in r.text
    assert actor.drafts == [], "a refused import still wrote to the draft"


@pytest.mark.parametrize("state", [TaskState.ACCEPTED])
def test_annotations_can_only_be_imported_into_a_CLAIMED_task(state: TaskState, _live_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing is annotating. Without this an accepted task could be rewritten after review — the
    same hole `save_draft` had, and it would be no less a hole for arriving as Arrow."""
    actor = _FakeActor(_task(state=state, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/import", content=_ipc([{"id": "a1", "shape_type": "bbox"}]))

    assert r.status_code == 409, r.text
    assert actor.drafts == []


def test_an_import_by_a_non_holder_is_403(_live_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="omar"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/import", content=_ipc([{"id": "a1", "shape_type": "bbox"}]))

    assert r.status_code == 403, r.text
    assert actor.drafts == []


def test_an_import_requires_can_annotate(_live_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(allow=False, seen=seen))

    r = client.post("/tasks/t1/import", content=_ipc([{"id": "a1", "shape_type": "bbox"}]))

    assert r.status_code == 403, r.text
    assert seen[0]["relation"] == "can_annotate"
    assert actor.drafts == []


def _rewritten(table: pa.Table, good: bytes, bad: bytes, *, framing: str = "stream") -> bytes:
    """`table` as an import body whose IPC framing still parses after `good` is overwritten with `bad`."""
    sink = pa.BufferOutputStream()
    opener = pa.ipc.new_stream if framing == "stream" else pa.ipc.new_file
    with opener(sink, table.schema) as writer:
        writer.write_table(table)
    body = sink.getvalue().to_pybytes()
    assert body.count(good) == 1, "the bytes to tamper with are not unique in the body"
    return body.replace(good, bad)


def _tampered(kind: pa.DataType, good: bytes, bad: bytes, *, framing: str = "stream") -> bytes:
    """Two shapes whose `text` column's buffers are rewritten."""
    values: list[Any] = [b"hello", b"world"] if kind == pa.binary() else ["hello", "world"]
    return _rewritten(pa.table({"shape_type": ["bbox", "bbox"], "text": pa.array(values, kind)}), good, bad, framing=framing)


_TEXT_OFFSETS = struct.pack("<iii", 0, 5, 10)
_NOT_UTF8 = b"\xff\xfe\xfd\xfc"
_END_OF_STREAM = b"\xff\xff\xff\xff\x00\x00\x00\x00"


def _stream(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _messages(body: bytes) -> list[bytes]:
    return [message.serialize().to_pybytes() for message in pa.ipc.MessageReader.open_stream(body)]


def _a_dictionary_batch_whose_id_names_no_field() -> bytes:
    """A schema with one dictionary column, followed by the dictionary batch another stream wrote for its id 1."""
    one = _messages(_stream(pa.table({"shape_type": ["bbox"], "label": pa.array(["cat"]).dictionary_encode()})))
    two = _messages(_stream(pa.table({"a": pa.array(["cat"]).dictionary_encode(), "b": pa.array(["dog"]).dictionary_encode()})))
    return b"".join([one[0], two[2], *one[1:], _END_OF_STREAM])


def _a_compressed_text_declaring(declared: int, codec: str) -> bytes:
    """The `text` values buffer's 8-byte uncompressed-length prefix rewritten, so the reader allocates `declared`."""
    table = pa.table({"shape_type": ["bbox"], "text": ["x" * 1003]})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema, options=pa.ipc.IpcWriteOptions(compression=codec)) as writer:
        writer.write_table(table)
    body = sink.getvalue().to_pybytes()
    at = body.rindex(struct.pack("<q", 1003))
    return body[:at] + struct.pack("<q", declared) + body[at + 8 :]


def _an_integer_narrower_than_eight_bits() -> bytes:
    """An int16 column whose schema declares a 4-bit width, at the byte where int16 and int32 schemas differ."""
    narrow, wide = (_stream(pa.table({"shape_type": ["bbox"], "char_start": pa.array([1], kind)})) for kind in (pa.int16(), pa.int32()))
    at = next(i for i, (a, b) in enumerate(zip(narrow, wide, strict=True)) if a != b)
    return narrow[:at] + bytes([4]) + narrow[at + 1 :]


#: Two batches of shapes whose `text` offsets differ, so the second batch's can be tampered with alone.
_TWO_BATCHES = pa.Table.from_batches(
    [
        pa.record_batch({"id": ["a1", "a2"], "shape_type": ["bbox", "bbox"], "text": ["hello", "world"]}),
        pa.record_batch({"id": ["a3", "a4"], "shape_type": ["bbox", "bbox"], "text": ["abc", "defghij"]}),
    ]
)


TAMPERED_BODIES = [
    pytest.param(b"this is not an arrow ipc stream", id="a-body-that-is-not-arrow"),
    pytest.param(_ipc([{"id": "a1", "shape_type": "bbox", "text": "hello"}])[:-20], id="a-stream-cut-inside-its-batch"),
    pytest.param(_tampered(pa.binary(), _TEXT_OFFSETS, struct.pack("<iii", 0, 5, 4096)), id="binary-offsets-4096-past-the-values-buffer"),
    pytest.param(
        _tampered(pa.binary(), _TEXT_OFFSETS, struct.pack("<iii", 0, 5, 65536), framing="file"), id="file-framed-binary-offsets-past-the-values-buffer"
    ),
    pytest.param(_rewritten(pa.table({"shape_type": ["bbox"], "extra": [{"zzzq": "x"}]}), b"zzzq", _NOT_UTF8), id="a-nested-field-name-that-is-not-utf8"),
    pytest.param(
        _rewritten(pa.table({"shape_type": ["bbox"]}).replace_schema_metadata({"kkkq": "v"}), b"kkkq", _NOT_UTF8), id="a-schema-metadata-key-that-is-not-utf8"
    ),
    pytest.param(
        _rewritten(pa.table({"shape_type": ["bbox"]}, schema=pa.schema([pa.field("shape_type", pa.string(), metadata={"k": "vvvq"})])), b"vvvq", _NOT_UTF8),
        id="a-field-metadata-value-that-is-not-utf8",
    ),
    pytest.param(_a_dictionary_batch_whose_id_names_no_field(), id="a-dictionary-batch-whose-id-names-no-field"),
    pytest.param(_a_compressed_text_declaring(2**50, "zstd"), id="a-zstd-buffer-declaring-2^50-bytes"),
    pytest.param(_an_integer_narrower_than_eight_bits(), id="an-integer-narrower-than-8-bits"),
    pytest.param(_stream(pa.table({"shape_type": ["bbox"]})) + _stream(pa.table({"shape_type": ["bbox"]})), id="two-streams-in-one-body"),
    pytest.param(_stream(_TWO_BATCHES)[:-20], id="a-stream-cut-inside-its-second-batch"),
]


@pytest.mark.parametrize("body", TAMPERED_BODIES)
def test_a_tampered_arrow_body_is_refused_and_nothing_is_imported(body: bytes, _live_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Framing that parses says nothing about the buffers or names inside it.

    `to_pylist` follows the body's own offsets: one past its values buffer becomes a shape's text
    holding process memory, which the draft stores and the route returns, and one that decreases
    aborts the process. Every such body answers the import's 400, and the draft is never written.
    """
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True), raise_server_exceptions=False)

    r = client.post("/tasks/t1/import", content=body)

    assert r.status_code == 400, r.text
    assert "not valid Arrow IPC" in r.text, f"refused by something other than the decoder: {r.text}"
    assert actor.drafts == [], "a refused import still wrote to the draft"


def test_every_batch_of_an_import_is_imported(_live_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/import", content=_stream(_TWO_BATCHES))

    assert r.status_code == 200, r.text
    assert [(s["shape_id"], s["text"]) for s in actor.drafts[0]["shapes"]] == [("a1", "hello"), ("a2", "world"), ("a3", "abc"), ("a4", "defghij")]


# --------------------------------------------------------------------------------------------------
# The assignee is TOLD — the annotator's first control-plane emission
# --------------------------------------------------------------------------------------------------


class _RecordingControl:
    """Captures what reached the control bus. Structural stand-in for `ControlEmitter`."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _app_with_control(control: _RecordingControl, *, allow: bool = True) -> FastAPI:
    from annotator.api.dependencies import get_control_emitter

    app = _app(allow=allow)
    app.dependency_overrides[get_control_emitter] = lambda: control
    return app


def _fire(app: FastAPI, actor: _FakeActor, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> Any:
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    return TestClient(app).post("/tasks/t1/events", json=body)


def test_acting_on_your_own_task_tells_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    """A holder releasing their own task is looking at the response that says so. An inbox row would be
    a second copy of something they just did — the plane's standing exclusion for outcomes the caller
    already has synchronously."""
    control = _RecordingControl()
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    resp = _fire(_app_with_control(control), actor, monkeypatch, {"event": "release"})

    assert resp.status_code == 200
    assert control.events == []


@pytest.mark.parametrize("event", ["claim"])
def test_edges_with_no_named_audience_emit_nothing(event: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_NOTIFIED_EDGES` is a whitelist on purpose. Emitting on every edge would put the annotator's own
    claims and submissions in their own inbox, which is how a bell stops being read."""
    control = _RecordingControl()
    state = TaskState.UNASSIGNED if event == "claim" else TaskState.CLAIMED
    actor = _FakeActor(_task(state=state, assignee=None if event == "claim" else SUBJECT))
    resp = _fire(_app_with_control(control), actor, monkeypatch, {"event": event})

    assert resp.status_code == 200
    assert control.events == []


# --------------------------------------------------------------------------------------------------
# The other DEPARTURE edges — work leaving a named person's hands
# --------------------------------------------------------------------------------------------------
#
# Twelve edges in `TASK_EDGES` take a task out of somebody's hands and, until this landed, exactly one
# of them (`release`) said so. `assign`/`release` are not a special pair — they were simply the two
# that got wired first. The audience for the review-side edges is `task.submitted_by`, which (unlike
# `assignee`, nulled one line into the actor turn) is written once and never cleared, so it is
# readable at every one of them.
#
# A DISTINCT ACTION PER EDGE, not a reused `task_unassigned`. The reason string IS the user-visible
# row label in the bell, so telling somebody their reviewed work was "unassigned" would be a worse
# answer than the silence it replaces.


def test_request_changes_tells_the_person_who_must_REDO_the_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """The audience is the SUBMITTER, and AUTHOR-style targeting inverts it: the actor here is the
    reviewer, who already knows what they just did. The person with work to do hears nothing today —
    and the review note that says WHY was written for exactly them."""
    control = _RecordingControl()
    actor = _FakeActor(_task(state=TaskState.IN_REVIEW, submitted_by="dave"))
    resp = _fire(_app_with_control(control), actor, monkeypatch, {"event": "request_changes"})

    assert resp.status_code == 200, resp.text
    assert [(e.action, e.extra["subject"]) for e in control.events] == [("task_changes_requested", "user:dave")]


def test_reopening_your_OWN_submission_tells_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same standing exclusion the assign/release pair already applies: the caller is looking at the
    response that says so, and a row would be a second copy of something they just did."""
    control = _RecordingControl()
    actor = _FakeActor(_task(state=TaskState.ACCEPTED, submitted_by=SUBJECT))
    resp = _fire(_app_with_control(control), actor, monkeypatch, {"event": "reopen"})

    assert resp.status_code == 200, resp.text
    assert control.events == []
