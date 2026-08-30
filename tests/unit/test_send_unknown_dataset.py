"""`send` refuses an item whose media dataset does not resolve.

Removal is the escape hatch; this is what stops the trap being set. An item naming a dataset that
was renamed or removed cannot be opened on the canvas, so it can never be claimed, submitted or
skipped — and the publish precondition requires EVERY task terminal, so ONE of them wedges the whole
project. Creating it is the mistake, and send is where refusing it costs nothing: zero task actors
have been seeded yet.

The interesting cases are the two where refusing would be WRONG: an item that names no dataset at
all (it takes the backend default, which resolves by construction), and a registry that cannot be
consulted (not being able to check is not evidence of a problem).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import project_events as events_ep
from annotator.api.v1.endpoints.project_events import router
from service_kit.exceptions import NotFoundError, register_handlers
from service_kit.media.deps import get_state


class _FakeProjectActor:
    async def get(self) -> dict[str, Any] | None:
        return {"project_id": "p1", "tenant": "acme", "slug": "vasa", "state": "labeling", "consensus_n": 1}

    async def task_state_changed(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"task_id": payload["task_id"], "state": payload["state"], "counts": {}}

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"task_id": payload["task_id"], "created": True}

    async def send_many(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"results": [{"task_id": t["task_id"], "created": True} for t in payload["tasks"]], "counts": {}}


class _FakeTaskActor:
    def __init__(self) -> None:
        self.seeded: list[dict[str, Any]] = []

    async def seed(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.seeded.append(payload)
        return payload


class _Registry:
    def __init__(self, ids: list[str]) -> None:
        self._ids = ids

    def list_ids(self) -> list[str]:
        return self._ids


def _app(
    monkeypatch: pytest.MonkeyPatch,
    task_actor: _FakeTaskActor,
    *,
    known: list[str] | None = None,
    registry_broken: bool = False,
) -> FastAPI:
    monkeypatch.setattr(events_ep, "_project_proxy", lambda _p: _FakeProjectActor())
    monkeypatch.setattr(events_ep, "_task_proxy", lambda _t: task_actor)

    ids = known if known is not None else ["demo", "transcripts_v2"]

    def _handle(_state: Any, dataset_id: str | None = None) -> Any:
        if registry_broken:
            raise RuntimeError("registry unavailable")
        if dataset_id and dataset_id not in ids:
            raise NotFoundError(f"dataset {dataset_id!r} not found under .")
        return object()

    monkeypatch.setattr(events_ep, "dataset_handle", _handle)

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)

    async def checker(**_kw: Any) -> bool:
        return True

    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: "gina"

    class _State:
        registry = _Registry(ids)

    app.dependency_overrides[get_state] = lambda: _State()
    return app


def _items(*wheres: str | None) -> dict[str, Any]:
    return {
        "items": [
            {
                "source": {"kind": "chunks", "keys": [f"doc{i}/0/1"], "where": w},
                "media": {"kind": "image"},
            }
            for i, w in enumerate(wheres)
        ]
    }


def test_an_item_naming_an_UNKNOWN_dataset_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    task_actor = _FakeTaskActor()
    client = TestClient(_app(monkeypatch, task_actor))

    r = client.post("/projects/p1/items", json=_items("gone-away"))

    assert r.status_code == 409, r.text
    assert "gone-away" in r.text, "the refusal must name the dataset that does not exist"
    assert task_actor.seeded == [], "a task actor was seeded for an item that can never be opened"


def test_the_refusal_names_the_datasets_that_DO_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal nobody can act on is not enforcement — it is a wall."""
    client = TestClient(_app(monkeypatch, _FakeTaskActor(), known=["demo", "vasa"]))

    r = client.post("/projects/p1/items", json=_items("typo"))

    assert "demo" in r.text and "vasa" in r.text


def test_ONE_bad_item_refuses_the_WHOLE_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial send produces exactly the half-populated project this exists to prevent — and it
    costs zero seeded actors to refuse, the same argument the project-state check makes."""
    task_actor = _FakeTaskActor()
    client = TestClient(_app(monkeypatch, task_actor))

    r = client.post("/projects/p1/items", json=_items("demo", "gone-away", "demo"))

    assert r.status_code == 409, r.text
    assert task_actor.seeded == [], "the good items were sent anyway, half-populating the project"


def test_a_known_dataset_sends_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    task_actor = _FakeTaskActor()
    client = TestClient(_app(monkeypatch, task_actor))

    r = client.post("/projects/p1/items", json=_items("demo"))

    assert r.status_code == 201, r.text
    assert len(task_actor.seeded) == 1


def test_an_item_naming_NO_dataset_is_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent means the backend default, which resolves by construction. Refusing it would break
    every ordinary send to make a point about a case that cannot happen."""
    task_actor = _FakeTaskActor()
    client = TestClient(_app(monkeypatch, task_actor))

    r = client.post("/projects/p1/items", json=_items(None))

    assert r.status_code == 201, r.text
    assert len(task_actor.seeded) == 1


def test_an_UNVERIFIABLE_registry_lets_the_send_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not being able to check is not evidence of a problem.

    If the dataset plane cannot be consulted at all, refusing every send would take the labeling
    plane down for a guard rather than a gate — the canvas still reports the real 404 at read, and
    removal still exists. This is the one place the check yields.
    """
    task_actor = _FakeTaskActor()
    client = TestClient(_app(monkeypatch, task_actor, registry_broken=True))

    r = client.post("/projects/p1/items", json=_items("anything"))

    assert r.status_code == 201, r.text
    assert len(task_actor.seeded) == 1
