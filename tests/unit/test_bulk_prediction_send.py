"""Bulk labeling: a PREDICTION sent alongside an item (#42, steps 2-3).

Browse could always find keys and queue them. It could not say what they are. So "find five hundred
pages like this one and label them" queued five hundred blank tasks, and every one still had to be
opened and drawn on — the bulk surface was bulk in selection only.

The shape this pins is Label Studio's, deliberately: a PREDICTION is a pre-annotation attached to the
task, NOT the annotator's own work. It rides the task document (one write, at send), it is stamped
server-side, and it is checked against the project's taxonomy before a single actor is seeded. A
human still claims, corrects and submits — nothing here reaches the lakehouse unreviewed, because
nothing can.

Four properties carry the whole design, and a wrong implementation still looks right for all four:
the field must actually SURVIVE the wire (pydantic drops what it does not declare, in silence); the
sender must not be able to forge `source`; an invented label must be refused BEFORE anything is
seeded; and a prediction must never be a draft, because a draft is submittable and a prediction is
a suggestion.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import project_events as ev
from annotator.projects.models import ProjectState
from service_kit.exceptions import register_handlers
from service_kit.media.deps import get_state


SUBJECT = "henry"

#: A two-class taxonomy with tools declared, so both the closed-set check and the per-class tool
#: check have something to refuse. `tag` is the whole-item primitive bulk classification uses.
ONTOLOGY: dict[str, Any] = {
    "classes": [
        {"name": "letter", "tools": ["tag"]},
        {"name": "envelope", "tools": ["tag", "bbox"]},
    ]
}


class _FakeProject:
    def __init__(self, state: ProjectState = ProjectState.LABELING, *, ontology: dict[str, Any] | None = None, consensus_n: int = 1) -> None:
        self.state = state
        self.ontology = ONTOLOGY if ontology is None else ontology
        self.consensus_n = consensus_n
        self.sent: list[dict[str, Any]] = []

    async def get(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "project_id": "p1",
            "review_required": True,
            "lease_seconds": 900,
            "ontology": self.ontology,
            "consensus_n": self.consensus_n,
        }

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(payload)
        return {"task_id": payload["task_id"], "created": True, "counts": {}}

    async def send_many(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.extend(payload["tasks"])  # ANN-03: the batch door replaces per-task Send
        return {"results": [{"task_id": t["task_id"], "created": True} for t in payload["tasks"]], "counts": {}}


class _FakeTask:
    """Records what was seeded. `save_draft` exists ONLY so a test can prove it is never reached."""

    def __init__(self) -> None:
        self.seeded: list[dict[str, Any]] = []
        self.drafts: list[dict[str, Any]] = []

    async def seed(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.seeded.append(payload)
        return dict(payload)

    async def save_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.drafts.append(payload)
        return dict(payload)


def _client(project: _FakeProject, task: _FakeTask, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def checker(**_call: Any) -> bool:
        """Every relation granted — authorization is `test_project_event_endpoints`'s subject."""
        return True

    monkeypatch.setattr(ev, "_project_proxy", lambda _p: project)
    monkeypatch.setattr(ev, "_task_proxy", lambda _t: task)

    app = FastAPI()
    register_handlers(app)
    app.include_router(ev.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: SUBJECT
    app.dependency_overrides[get_state] = lambda: SimpleNamespace(registry=SimpleNamespace(list_ids=list))
    return TestClient(app)


def _item(prediction: list[dict[str, Any]] | None = None, *, task_id: str = "t1") -> dict[str, Any]:
    item: dict[str, Any] = {
        "task_id": task_id,
        "source": {"kind": "chunks", "keys": ["page-1"], "where": "vasa"},
        "media": {"kind": "image"},
    }
    if prediction is not None:
        item["prediction"] = prediction
    return item


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Any:
    def build(*, ontology: dict[str, Any] | None = None, consensus_n: int = 1) -> tuple[TestClient, _FakeProject, _FakeTask]:
        project = _FakeProject(ontology=ontology, consensus_n=consensus_n)
        task = _FakeTask()
        return _client(project, task, monkeypatch), project, task

    return build


# --------------------------------------------------------------------------------------------------
# It has to survive the wire at all
# --------------------------------------------------------------------------------------------------


def test_a_prediction_reaches_the_task_actor(wired: Any) -> None:
    """The defect this exists for. `SendItem` did not declare `prediction`, and pydantic drops an
    undeclared key WITHOUT erroring — so a bulk label would post 200, seed the tasks, and carry
    nothing. The send looked like it worked. Every queued item was still blank."""
    client, _project, task = wired()

    r = client.post("/projects/p1/items", json={"items": [_item([{"shape_type": "tag", "label": "letter"}])]})

    assert r.status_code == 201, r.text
    assert task.seeded, "no task was seeded at all"
    shapes = task.seeded[0].get("prediction") or []
    assert [s["label"] for s in shapes] == ["letter"], "the prediction did not survive onto the task"


def test_an_item_with_no_prediction_carries_none(wired: Any) -> None:
    """The ordinary send is unchanged — an empty list, never a fabricated shape."""
    client, _project, task = wired()

    r = client.post("/projects/p1/items", json={"items": [_item()]})

    assert r.status_code == 201, r.text
    assert (task.seeded[0].get("prediction") or []) == []


# --------------------------------------------------------------------------------------------------
# Provenance the sender cannot write
# --------------------------------------------------------------------------------------------------


def test_the_server_stamps_the_source(wired: Any) -> None:
    """`source` is how every later surface tells suggested work from drawn work. If the sender set
    it, a bulk action could stamp `human` on five hundred items nobody looked at and they would read
    as annotated for the rest of the corpus's life."""
    client, _project, task = wired()

    r = client.post("/projects/p1/items", json={"items": [_item([{"shape_type": "tag", "label": "letter"}])]})

    assert r.status_code == 201, r.text
    assert task.seeded[0]["prediction"][0]["source"] == "bulk"


def test_a_sender_supplied_source_is_ignored_not_honoured(wired: Any) -> None:
    """Same rule, asked as forgery. The wire model does not declare `source`, so this is dropped
    rather than overwritten — but the property that matters is what ends up stored."""
    client, _project, task = wired()

    r = client.post(
        "/projects/p1/items",
        json={"items": [_item([{"shape_type": "tag", "label": "letter", "source": "human"}])]},
    )

    assert r.status_code == 201, r.text
    assert task.seeded[0]["prediction"][0]["source"] == "bulk", "a caller forged human provenance"


def test_a_sender_cannot_pre_accept_by_supplying_task_state(wired: Any) -> None:
    """The existing guarantee, re-asserted for the new field: a prediction must not become a way in
    to the fields `SendItem` deliberately refuses."""
    client, _project, task = wired()

    r = client.post("/projects/p1/items", json={"items": [{**_item([{"shape_type": "tag", "label": "letter"}]), "state": "accepted"}]})

    assert r.status_code == 201, r.text
    assert task.seeded[0]["state"] == "unassigned"


# --------------------------------------------------------------------------------------------------
# The taxonomy is closed, and it closes BEFORE anything is written
# --------------------------------------------------------------------------------------------------


def test_a_label_outside_the_taxonomy_is_refused(wired: Any) -> None:
    """The closed-set property is the entire reason a class list is a first-class object. A bulk
    action is the worst possible place to leak an invented label: one click, five hundred rows."""
    client, _project, task = wired()

    r = client.post("/projects/p1/items", json={"items": [_item([{"shape_type": "tag", "label": "asdf"}])]})

    assert r.status_code == 409, r.text
    assert "asdf" in r.text, "the refusal must name the label that closed it"
    assert task.seeded == [], "tasks were seeded despite the refusal"


def test_a_tool_the_class_forbids_is_refused(wired: Any) -> None:
    """`letter` declares `tag` only. Membership is not just the label — it is the label WITH the
    primitive it was drawn with."""
    client, _project, task = wired()

    r = client.post("/projects/p1/items", json={"items": [_item([{"shape_type": "bbox", "label": "letter"}])]})

    assert r.status_code == 409, r.text
    assert task.seeded == []


def test_the_refusal_happens_before_ANY_item_is_seeded(wired: Any) -> None:
    """A per-item check inside the seed loop would leave the good items queued and the bad ones not
    — a half-applied bulk action, which is worse than a refused one because nobody can tell which
    half landed. The whole send is validated first."""
    client, _project, task = wired()

    good = _item([{"shape_type": "tag", "label": "letter"}], task_id="t1")
    bad = _item([{"shape_type": "tag", "label": "nope"}], task_id="t2")

    r = client.post("/projects/p1/items", json={"items": [good, bad]})

    assert r.status_code == 409, r.text
    assert task.seeded == [], "the first item was seeded before the second was validated"


def test_an_unconstrained_project_accepts_any_label(wired: Any) -> None:
    """An empty taxonomy constrains nothing — the same posture every other surface takes, rather
    than a bulk-only rule that would surprise."""
    client, _project, task = wired(ontology={})

    r = client.post("/projects/p1/items", json={"items": [_item([{"shape_type": "tag", "label": "anything"}])]})

    assert r.status_code == 201, r.text
    assert task.seeded[0]["prediction"][0]["label"] == "anything"


# --------------------------------------------------------------------------------------------------
# A prediction is not a draft
# --------------------------------------------------------------------------------------------------


def test_a_prediction_never_becomes_a_draft(wired: Any) -> None:
    """The load-bearing distinction. A draft is submittable; a prediction is a suggestion. Writing
    one as the other would let a bulk action produce work that walks into review having been drawn
    by nobody — and `save_draft` refuses a non-CLAIMED task precisely so this cannot be done by
    accident. Routing around that refusal is the mistake this test exists to catch."""
    client, _project, task = wired()

    r = client.post("/projects/p1/items", json={"items": [_item([{"shape_type": "tag", "label": "letter"}])]})

    assert r.status_code == 201, r.text
    assert task.drafts == [], "the prediction was written as a draft"


def test_the_task_is_still_unassigned_after_a_predicted_send(wired: Any) -> None:
    """Predicting does not claim. The item goes into the queue like any other, for a person."""
    client, _project, task = wired()

    client.post("/projects/p1/items", json={"items": [_item([{"shape_type": "tag", "label": "letter"}])]})

    assert task.seeded[0]["state"] == "unassigned"
    assert task.seeded[0]["assignee"] is None


# --------------------------------------------------------------------------------------------------
# Consensus
# --------------------------------------------------------------------------------------------------


def test_every_replica_carries_the_same_prediction(wired: Any) -> None:
    """Consensus asks several people the same question. Handing some of them a suggestion and not
    others would make their disagreement an artefact of the send rather than of the images."""
    client, _project, task = wired(consensus_n=3)

    r = client.post("/projects/p1/items", json={"items": [_item([{"shape_type": "tag", "label": "letter"}])]})

    assert r.status_code == 201, r.text
    assert len(task.seeded) == 3, "consensus_n=3 did not seed three replicas"
    assert all((t.get("prediction") or [{}])[0].get("label") == "letter" for t in task.seeded)
