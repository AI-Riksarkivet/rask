"""The wire-name translation — the bug the first live drive of the projects plane found.

Dapr's `ActorProxy.__getattr__` dispatches from a map keyed by the WIRE name
(`@actormethod(name="ListProjects")`), not the Python attribute. Unit-test fakes implement the
Python names, so every mocked test stayed green while every real cross-actor call raised
`AttributeError` against the sidecar. These tests pin the translation AND sweep every call site
onto it, so a new pythonic `proxy.some_method()` against a raw `ActorProxy` cannot land again.
"""

from __future__ import annotations

from typing import Any

import pytest
from annotator.projects.actor import AnnotationTaskActorInterface
from annotator.projects.project_actor import AnnotationProjectActorInterface
from annotator.projects.proxies import TypedActorProxy
from annotator.projects.tenant_actor import TenantProjectsActorInterface


class _WireOnlyProxy:
    """Behaves like the real `ActorProxy`: ONLY wire names resolve."""

    def __init__(self, wire_names: set[str]) -> None:
        self._wire = wire_names
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name not in self._wire:
            raise AttributeError(f"has no attribute {name}")

        async def _call(*args: Any) -> dict[str, Any]:
            self.calls.append(name)
            return {}

        return _call


def _wire_names(interface: type) -> set[str]:
    return {v.__actormethod__ for v in interface.__dict__.values() if hasattr(v, "__actormethod__")}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interface", "python_name", "wire_name"),
    [
        (AnnotationProjectActorInterface, "list_tasks", "ListTasks"),
        (AnnotationProjectActorInterface, "record_publish", "RecordPublish"),
        (AnnotationProjectActorInterface, "note_progress", "NoteProgress"),
        (AnnotationProjectActorInterface, "adjudicate", "Adjudicate"),
        (AnnotationTaskActorInterface, "get_draft", "GetDraft"),
        (AnnotationTaskActorInterface, "save_draft", "SaveDraft"),
        (TenantProjectsActorInterface, "list_projects", "ListProjects"),
        (TenantProjectsActorInterface, "register", "Register"),
    ],
)
async def test_python_names_reach_the_wire_names(interface: type, python_name: str, wire_name: str) -> None:
    raw = _WireOnlyProxy(_wire_names(interface))
    proxy = TypedActorProxy(raw, interface)

    await getattr(proxy, python_name)({})

    assert raw.calls == [wire_name]


def test_an_undeclared_method_fails_loudly() -> None:
    proxy = TypedActorProxy(_WireOnlyProxy(set()), TenantProjectsActorInterface)
    with pytest.raises(AttributeError, match="declares no method"):
        _ = proxy.not_a_method


def test_no_call_site_builds_a_raw_actor_proxy() -> None:
    """The sweep: every proxy in the plane goes through `typed_proxy`. A raw `ActorProxy.create`
    outside proxies.py reintroduces the wire-name mismatch for whatever pythonic call follows it."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "services/annotator/src/annotator"
    offenders = [str(path.relative_to(root)) for path in root.rglob("*.py") if "proxies.py" not in str(path) and "ActorProxy.create" in path.read_text()]
    assert offenders == [], f"raw ActorProxy.create outside proxies.py: {offenders}"
