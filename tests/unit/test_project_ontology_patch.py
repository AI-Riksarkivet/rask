"""`PATCH /projects/{id}/ontology` — the task definition is editable, and gated on three axes.

There was no route at all: an ontology could be set at create and never again, so a taxonomy typo
meant a new project. The gates are the interesting part, and each one exists for a different reason:

* **Permission.** Editing the definition changes what every FUTURE item promises downstream, so it
  is a `can_manage` act, not a labeling one — the same relation `open`/`freeze`/`publish` carry.
* **State.** Past `frozen` the answer set is closed and a publish is being prepared against it, so
  an edit could only be ignored (every remaining item already captured its copy) or misleading —
  the run facet would report a taxonomy no task was ever judged against.
* **Shape.** The document is validated as a UNIT, so a contradictory edit is refused rather than
  half-applied. This is precisely what the `label_schema` + `template` pair could not do.
"""

from __future__ import annotations

from typing import Any

import pytest
from annotator.api.security import current_subject, get_checker, get_fga_client
from annotator.api.v1.endpoints import projects as projects_ep
from annotator.api.v1.endpoints.projects import MANAGE_RELATION, router
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import register_handlers


def _project(state: str = "draft", **extra: Any) -> dict[str, Any]:
    return {
        "project_id": "p1",
        "tenant": "acme",
        "slug": "vasa",
        "state": state,
        **extra,
    }


class _FakeProjectActor:
    def __init__(self, doc: dict[str, Any] | None) -> None:
        self._doc = doc
        self.written: list[dict[str, Any]] = []

    async def get(self) -> dict[str, Any] | None:
        return self._doc

    async def set_ontology(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.written.append(payload)
        assert self._doc is not None
        return {**self._doc, "ontology": payload["ontology"]}


def _app(actor: _FakeProjectActor, monkeypatch: pytest.MonkeyPatch, *, allow: bool = True, record: list | None = None) -> FastAPI:
    monkeypatch.setattr(projects_ep, "_create_actor", lambda _p: actor)

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        if record is not None:
            record.append({"user": user, "relation": relation, "obj": obj})
        return allow

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: "gina"
    app.dependency_overrides[get_fga_client] = lambda: None
    return app


BBOX = {"kind": "object-detection", "classes": [{"name": "region", "tools": ["bbox"]}]}


def test_a_manager_replaces_the_ontology(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeProjectActor(_project())
    client = TestClient(_app(actor, monkeypatch))

    r = client.patch("/projects/p1/ontology", json={"ontology": BBOX})

    assert r.status_code == 200, r.text
    assert r.json()["ontology"]["classes"][0]["name"] == "region"
    assert actor.written, "the route answered 200 without writing anything"


def test_the_gate_is_can_manage_not_can_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading a project is not licence to redefine what its work means."""
    seen: list[dict[str, Any]] = []
    actor = _FakeProjectActor(_project())
    client = TestClient(_app(actor, monkeypatch, record=seen))

    client.patch("/projects/p1/ontology", json={"ontology": BBOX})

    assert [c["relation"] for c in seen] == [MANAGE_RELATION]
    assert seen[0]["obj"] == "annotation_project:p1"


def test_a_denied_caller_gets_403_and_writes_NOTHING(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeProjectActor(_project())
    client = TestClient(_app(actor, monkeypatch, allow=False))

    r = client.patch("/projects/p1/ontology", json={"ontology": BBOX})

    assert r.status_code == 403, r.text
    assert actor.written == [], "a refused caller still reached the actor"


@pytest.mark.parametrize("state", ["draft", "labeling"])
def test_editable_while_the_project_can_still_receive_work(state: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`labeling` is deliberately included. Items already sent captured their own copy, so work in
    review is judged by the contract it was issued under — which is what makes this safe at all."""
    actor = _FakeProjectActor(_project(state))
    client = TestClient(_app(actor, monkeypatch))

    assert client.patch("/projects/p1/ontology", json={"ontology": BBOX}).status_code == 200


@pytest.mark.parametrize("state", ["frozen", "publishing", "published", "archived"])
def test_refused_once_the_answer_set_is_closed(state: str, monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeProjectActor(_project(state))
    client = TestClient(_app(actor, monkeypatch))

    r = client.patch("/projects/p1/ontology", json={"ontology": BBOX})

    assert r.status_code == 409, r.text
    assert state in r.text, "the refusal must name the state that caused it"
    assert actor.written == []


def test_a_contradictory_ontology_is_refused_WHOLE(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validated as a unit, so nothing is half-applied.

    A relation pointing at an undeclared class is the exact cross-check the two-object model had
    nowhere to run — `label_schema` and `template` were separate fields no validator ever saw
    together.
    """
    actor = _FakeProjectActor(_project())
    client = TestClient(_app(actor, monkeypatch))

    r = client.patch(
        "/projects/p1/ontology",
        json={"ontology": {"classes": [{"name": "key"}], "relations": [{"name": "answers", "to_classes": ["value"]}]}},
    )

    assert r.status_code == 422, r.text
    assert "value" in r.text
    assert actor.written == [], "a contradictory ontology reached the store"


def test_an_unknown_project_is_404_not_a_silent_create(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeProjectActor(None)
    client = TestClient(_app(actor, monkeypatch))

    assert client.patch("/projects/p1/ontology", json={"ontology": BBOX}).status_code == 404
    assert actor.written == []


def test_the_replace_is_WHOLE_DOCUMENT_so_an_omitted_list_clears_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason this is not a merge, pinned.

    A partial merge would have to answer "what does an absent `classes` mean" — cleared, or
    unchanged? — and the two readings differ by an entire taxonomy. Whole-document has one answer,
    and it is the one the caller can see in the request body.
    """
    actor = _FakeProjectActor(_project(ontology=BBOX))
    client = TestClient(_app(actor, monkeypatch))

    r = client.patch("/projects/p1/ontology", json={"ontology": {"kind": "object-detection"}})

    assert r.status_code == 200, r.text
    assert r.json()["ontology"]["classes"] == []
