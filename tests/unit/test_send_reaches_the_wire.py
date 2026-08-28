"""The send door, driven through the WIRE names — the trap a mocked endpoint test cannot see.

Dapr's `ActorProxy.__getattr__` dispatches from a map keyed by the `@actormethod(name=…)` WIRE name,
not the Python attribute. Every endpoint test in this estate patches `_project_proxy` to a fake that
implements the PYTHON names, so a handler calling a method the interface never declared — or one
whose wire name disagrees — stays green in every suite and raises `AttributeError` against the real
sidecar. That is not hypothetical: it is what the first live drive of this plane found, and it is
exactly the shape `send_many`/`SendMany` reintroduces (open_python-audit ANN-03).

`tests/unit/test_actor_proxy_names.py` pins the translation for a hand-written list of methods. This
file pins it for the SEND PATH end to end (the handler, through the real `TypedActorProxy`, onto a
proxy that resolves wire names only) and then closes the list: every `@actormethod` on the project
interface must resolve and must be implemented, so the next method added is covered without anyone
remembering to add a row.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import project_events as ev
from annotator.projects.actor import AnnotationTaskActor, AnnotationTaskActorInterface
from annotator.projects.project_actor import AnnotationProjectActor, AnnotationProjectActorInterface
from annotator.projects.proxies import TypedActorProxy
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import register_handlers
from service_kit.media.deps import get_state


class _WireOnlyProxy:
    """Behaves like the real `ActorProxy`: ONLY wire names resolve, and every call is recorded."""

    def __init__(self, wire_names: set[str], replies: dict[str, Any] | None = None) -> None:
        self._wire = wire_names
        self._replies = replies or {}
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name not in self._wire:
            raise AttributeError(f"has no attribute {name}")

        async def _call(*args: Any) -> Any:
            self.calls.append(name)
            reply = self._replies.get(name)
            return reply(*args) if callable(reply) else reply

        return _call


def _wire_names(interface: type) -> set[str]:
    return {v.__actormethod__ for v in interface.__dict__.values() if hasattr(v, "__actormethod__")}


def test_the_send_door_only_ever_names_declared_wire_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /items over proxies that resolve WIRE names only.

    A handler calling an undeclared Python name dies with `AttributeError` here — a 500 — the same
    way it would against daprd, instead of passing against a fake that answers to anything.
    """
    project_raw = _WireOnlyProxy(
        _wire_names(AnnotationProjectActorInterface),
        replies={
            "Get": lambda: {"state": "labeling", "project_id": "p1", "review_required": True, "lease_seconds": 900},
            "SendMany": lambda body: {"results": [{"task_id": t["task_id"], "created": True} for t in body["tasks"]], "counts": {}},
        },
    )
    task_raw = _WireOnlyProxy(_wire_names(AnnotationTaskActorInterface), replies={"Seed": lambda body: {**body}})

    monkeypatch.setattr(ev, "_project_proxy", lambda _p: TypedActorProxy(project_raw, AnnotationProjectActorInterface))
    monkeypatch.setattr(ev, "_task_proxy", lambda _t: TypedActorProxy(task_raw, AnnotationTaskActorInterface))

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        return True

    app = FastAPI()
    register_handlers(app)
    app.include_router(ev.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: "henry"
    app.dependency_overrides[get_state] = lambda: SimpleNamespace(registry=SimpleNamespace(list_ids=list))

    items = [{"task_id": f"i{k}", "source": {"kind": "chunks", "keys": [f"k{k}"]}, "media": {"kind": "image"}} for k in range(3)]
    response = TestClient(app).post("/projects/p1/items", json={"items": items})

    assert response.status_code == 201, response.text
    assert task_raw.calls == ["Seed", "Seed", "Seed"]
    # ONE index write, and it is the batch door. Both halves matter: the wire name is what daprd
    # routes on, and the count is the defect this method exists to fix.
    assert project_raw.calls == ["Get", "SendMany"], project_raw.calls


@pytest.mark.parametrize(
    ("interface", "implementation"),
    [(AnnotationProjectActorInterface, AnnotationProjectActor), (AnnotationTaskActorInterface, AnnotationTaskActor)],
)
def test_every_declared_actor_method_resolves_and_is_implemented(interface: type, implementation: type) -> None:
    """The list-free half. A new `@actormethod` is pinned by existing here rather than by someone
    remembering to add a parametrize row — which is how `SendMany` would otherwise have shipped
    unpinned."""
    raw = _WireOnlyProxy(_wire_names(interface))
    proxy = TypedActorProxy(raw, interface)

    declared = {name: value.__actormethod__ for name, value in interface.__dict__.items() if hasattr(value, "__actormethod__")}
    assert declared, f"{interface.__name__} declares no actor methods"

    for python_name, wire_name in declared.items():
        assert getattr(proxy, python_name) is not None, f"{python_name} does not resolve through the typed proxy"
        assert wire_name, f"{interface.__name__}.{python_name} is declared with no wire name"
        # The implementation must OVERRIDE the interface stub, or every call raises NotImplementedError
        # inside the actor — a 500 from the sidecar with nothing naming the method.
        assert python_name in implementation.__dict__ or any(python_name in base.__dict__ for base in implementation.__mro__ if base is not interface), (
            f"{implementation.__name__} does not implement {python_name}"
        )
