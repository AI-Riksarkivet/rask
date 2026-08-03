"""Typed actor proxies that speak PYTHON names over Dapr's WIRE names.

`ActorProxy.__getattr__` dispatches from a map keyed by the `@actormethod(name=...)` WIRE name
(`ListProjects`), not the Python attribute (`list_projects`) — so `proxy.list_projects()` raises
`AttributeError` against a real sidecar while unit tests, whose fakes implement the Python names,
stay green. The first live drive of this plane found it: every cross-actor call here was broken
in-cluster, invisibly, because `_report_state` swallows its failure by design.

This adapter reads the interface's own `@actormethod` metadata, so call sites (and the saga's
`ProjectHandle`/`TaskHandle` protocols) keep the Python names and the wire names stay the fixed
routing ids the interface docstrings promise. One translation, sourced from the single place the
mapping is declared — it cannot drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast


if TYPE_CHECKING:
    from dapr.actor import ActorInterface


class TypedActorProxy:
    """Wraps an `ActorProxy`, translating Python method names to the interface's wire names."""

    def __init__(self, proxy: Any, interface: type) -> None:
        self._proxy = proxy
        self._interface = interface

    def __getattr__(self, name: str) -> Any:
        declared = getattr(self._interface, name, None)
        if declared is None:
            raise AttributeError(f"{self._interface.__name__} declares no method {name!r}")
        wire = getattr(declared, "__actormethod__", name)
        return getattr(self._proxy, wire)


def typed_proxy(actor_type: str, actor_id: str, interface: type) -> Any:
    """A `TypedActorProxy` over a fresh sidecar channel — the lazy-import pattern every call site
    already uses (`ActorProxy` opens a channel, so importing it at module scope would make merely
    importing an endpoint module require daprd)."""
    from dapr.actor import ActorId, ActorProxy  # noqa: PLC0415 - deliberate, see docstring

    # `cast`, not a signature change: callers hand the concrete interface class, and narrowing the
    # parameter to `type[ActorInterface]` would force every unit-test fake to inherit dapr's base.
    return TypedActorProxy(ActorProxy.create(actor_type, ActorId(actor_id), cast("type[ActorInterface]", interface)), interface)
