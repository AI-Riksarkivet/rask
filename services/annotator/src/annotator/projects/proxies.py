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

import re
from typing import TYPE_CHECKING, Any, Protocol, cast


if TYPE_CHECKING:
    from dapr.actor import ActorInterface


class WireDispatch(Protocol):
    """What the wrapper needs from the wrapped proxy: WIRE-named attributes resolving to calls.

    A Protocol rather than `ActorProxy` for the same reason `interface` stays a bare `type` (see
    `typed_proxy`): unit-test fakes implement the wire-attribute contract without inheriting dapr's
    class, and the contract really is just "attribute access by wire name".
    """

    def __getattr__(self, name: str, /) -> Any: ...


class TypedActorProxy:
    """Wraps an `ActorProxy`, translating Python method names to the interface's wire names."""

    def __init__(self, proxy: WireDispatch, interface: type) -> None:
        self._proxy = proxy
        self._interface = interface

    def __getattr__(self, name: str) -> Any:
        declared = getattr(self._interface, name, None)
        if declared is None:
            raise AttributeError(f"{self._interface.__name__} declares no method {name!r}")
        wire = getattr(declared, "__actormethod__", name)
        if wire is None:
            # PRESENT-and-None is not the same as absent, and only the second reaches the default
            # above. A decorator that recorded no wire name therefore handed `None` to `getattr`,
            # which raises "attribute name must be string" from inside the proxy — a TypeError about
            # strings, naming neither the interface nor the method, for what is a mis-declared actor
            # method. Refuse here, by name.
            raise AttributeError(f"{self._interface.__name__}.{name} is declared with no actor wire name")
        return _translating(getattr(self._proxy, wire))


def _translating(call: Any) -> Any:
    """Re-raise an actor-side `IllegalTransition` as itself on THIS side of the sidecar.

    Dapr serialises an actor's exception into an HTTP 500 body and the SDK raises `DaprHttpError`,
    so every `except IllegalTransition` in the endpoints — the code that turns a refused transition
    into a **409 with the reason** — silently stopped matching the moment the check moved into an
    actor. A precondition that reads "template classification allows tools ['tag'] — shape … is
    bbox" reached the UI as a bare `500 Internal Server Error`: the guard fired, and the user was
    told nothing. Found by driving a template violation against the live service.

    Translating HERE rather than in each endpoint is what keeps it fixed: this proxy is the single
    seam every actor call already goes through, so the endpoints' existing handlers work as written
    and a future actor-side precondition inherits the behaviour for free.
    """

    async def invoke(*args: Any, **kwargs: Any) -> Any:
        from annotator.projects.machines import IllegalTransition  # noqa: PLC0415 - import cycle

        try:
            return await call(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised unless it is the one shape we own
            message = str(exc)
            if "IllegalTransition" not in message:
                raise
            # Rebuild it through its real constructor, from the message the actor formatted:
            #   illegal {kind} transition: {event!r} is not permitted from {state!r}
            # so `kind`/`state`/`event` survive the hop and the re-raised error is indistinguishable
            # from a locally-raised one. Unparseable (a message shape that changed) degrades to the
            # whole text as the event — still a 409 carrying the reason, never a silent 500.
            #
            # UNESCAPE first: the text arrives nested inside Dapr's JSON envelope inside the SDK's
            # own message, so the quotes are escaped several layers deep. Parsing the raw string
            # matched nothing and surfaced the entire envelope as the user-facing reason.
            text = message
            while True:
                unescaped = text.replace("\\\\", "\\").replace('\\"', '"').replace("\\'", "'")
                if unescaped == text:
                    break
                text = unescaped
            parsed = re.search(r"illegal (\w+) transition: [\"'](.+?)[\"'] is not permitted from (.+?)(?:['\"]\)|$)", text, re.DOTALL)
            if not parsed:
                raise IllegalTransition("task", "unknown", text) from exc
            # The state arrives as the enum's repr (`<TaskState.CLAIMED: 'claimed'>`) rather than a
            # bare value, so take the quoted value when there is one.
            state = re.search(r"'([^']+)'", parsed.group(3))
            raise IllegalTransition(parsed.group(1), state.group(1) if state else parsed.group(3).strip(), parsed.group(2)) from exc

    return invoke


def typed_proxy(actor_type: str, actor_id: str, interface: type) -> Any:
    """A `TypedActorProxy` over a fresh sidecar channel — the lazy-import pattern every call site
    already uses (`ActorProxy` opens a channel, so importing it at module scope would make merely
    importing an endpoint module require daprd)."""
    from dapr.actor import ActorId, ActorProxy  # noqa: PLC0415 - deliberate, see docstring

    # `cast`, not a signature change: callers hand the concrete interface class, and narrowing the
    # parameter to `type[ActorInterface]` would force every unit-test fake to inherit dapr's base.
    return TypedActorProxy(ActorProxy.create(actor_type, ActorId(actor_id), cast("type[ActorInterface]", interface)), interface)
