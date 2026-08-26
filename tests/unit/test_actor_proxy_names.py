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


# --------------------------------------------------------------------------------------------------
# A wire name that is None must fail HERE, with the method's name in the message
# --------------------------------------------------------------------------------------------------


class _NamelessInterface:
    """An interface whose decorator recorded no wire name.

    Not a fiction: `@actormethod(name=...)` writes `__actormethod__` unconditionally, so a decorator
    invoked without a name — or refactored to compute one and returning None on some path — leaves the
    attribute PRESENT and None. `getattr(declared, "__actormethod__", name)` then skips its own
    default (the default only applies when the attribute is ABSENT) and hands None onward.
    """

    def save_draft(self) -> None: ...


# `setattr`, not a suppression comment: the mypy-style one is not honoured by `ty` (and writing
# it here, even inside prose, is itself parsed as one). This mirrors what the decorator does anyway —
# write the attribute at runtime.
setattr(_NamelessInterface.save_draft, "__actormethod__", None)  # noqa: B010 — the point is the dynamic write


@pytest.mark.parametrize(
    "proxy_factory",
    [
        pytest.param(lambda: __import__("annotator.projects.proxies", fromlist=["TypedActorProxy"]).TypedActorProxy, id="annotator"),
        pytest.param(lambda: __import__("notifications.proxies", fromlist=["TypedActorProxy"]).TypedActorProxy, id="notifications"),
    ],
)
def test_a_None_wire_name_is_refused_by_name_not_passed_to_getattr(proxy_factory: Any) -> None:
    """`getattr(self._proxy, None)` raises `TypeError: attribute name must be string`, from inside the
    proxy, naming neither the interface nor the method. The caller sees a type error about strings for
    what is actually a mis-declared actor method.

    Parametrized across BOTH proxies deliberately: the two files carry the identical line, and a spot
    fix on one leaves the same hole in the other — which is how this shape reappears.
    """
    proxy_cls = proxy_factory()
    typed = proxy_cls(_WireOnlyProxy({"save_draft"}), _NamelessInterface)

    with pytest.raises(Exception) as caught:  # noqa: PT011 — the TYPE is what is under test
        _ = typed.save_draft

    assert not isinstance(caught.value, TypeError), f"the None wire name reached getattr(): {caught.value!r}"
    assert "save_draft" in str(caught.value), f"the refusal does not name the method: {caught.value!r}"
    assert "_NamelessInterface" in str(caught.value), f"the refusal does not name the interface: {caught.value!r}"
