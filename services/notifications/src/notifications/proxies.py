"""Typed actor proxies that speak PYTHON names over Dapr's WIRE names, plus the one id derivation.

`ActorProxy.__getattr__` dispatches from a map keyed by the `@actormethod(name=...)` WIRE name
(`MarkSeen`), not the Python attribute (`mark_seen`) — so `proxy.mark_seen()` raises `AttributeError`
against a real sidecar while unit tests, whose fakes implement the Python names, stay green. The
annotator's first live drive of its projects plane found exactly that, invisibly, and the fix is this
adapter: it reads the interface's own `@actormethod` metadata, so call sites keep the Python names and
the wire names stay the fixed routing ids the interface promises. One translation, sourced from the
single place the mapping is declared, so it cannot drift.

Every call into an inbox goes through :func:`inbox_for`, and every inbox id through
:func:`inbox_actor_id`. Nothing else in this service may build an `ActorProxy` or encode a subject —
`tests/test_actor_proxies.py` sweeps for both.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from notifications.errors import InboxUnreadable
from notifications.inbox_actor import INBOX_ACTOR_TYPE, InboxActorInterface
from service_kit.governed.user_state import DAPR_APP_ID_SEPARATOR, encode_subject


if TYPE_CHECKING:
    from dapr.actor import ActorInterface


logger = logging.getLogger(__name__)


#: One invocation across the sidecar. The argument list is deliberately unconstrained — the interface
#: is what declares each method's shape, and re-declaring it here would be a second place to keep in
#: step — but the RESULT is not: every actor method in this plane answers with a JSON object.
type ActorCall = Callable[..., Awaitable[dict[str, Any]]]


def inbox_actor_id(subject: str) -> str:
    """The actor id for one subject's inbox: base64url of the VERIFIED token sub, padding stripped.

    Not percent-encoding: the key travels in the sidecar's URL path, which is decoded on arrival, so a
    `%7C%7C` would become Dapr's reserved `||` again at the store. base64url cannot express `|` at all,
    whatever an identity provider puts in `sub`, and it stays reversible so an operator can read a row
    back to a person. The separator guard below outlives the encoding choice — it is the thing that
    must stay true if the encoding is ever revisited.

    Raises:
        ValueError: If `subject` is empty (there is no anonymous inbox), or if the id could ever carry
            Dapr's reserved app-id separator.
    """
    if not subject.strip():
        raise ValueError("an inbox requires a non-empty verified subject")
    actor_id = encode_subject(subject)
    if DAPR_APP_ID_SEPARATOR in actor_id:
        raise ValueError(f"inbox actor id {actor_id!r} contains Dapr's reserved app-id separator {DAPR_APP_ID_SEPARATOR!r} — the sidecar would reject it")
    return actor_id


class TypedActorProxy:
    """Wraps an `ActorProxy`, translating Python method names to the interface's wire names."""

    def __init__(self, proxy: object, interface: type) -> None:
        self._proxy = proxy
        self._interface = interface

    def __getattr__(self, name: str) -> ActorCall:
        declared = getattr(self._interface, name, None)
        if declared is None:
            raise AttributeError(f"{self._interface.__name__} declares no method {name!r}")
        wire = getattr(declared, "__actormethod__", name)
        return _translating(getattr(self._proxy, wire))


def _translating(call: ActorCall) -> ActorCall:
    """Re-raise an actor-side `InboxUnreadable` as itself on THIS side of the sidecar.

    Dapr serialises an actor's exception into an HTTP 500 body and the SDK raises `DaprHttpError`, so
    without this every `except InboxUnreadable` in the routes stops matching the moment the check runs
    inside an actor: a record this service deliberately refuses to serve would reach the browser as a
    bare 500, which reads as "the notification service is broken" rather than "this inbox record is
    not servable". Translating HERE keeps the routes' handlers working as written and gives any future
    actor-side refusal the same behaviour for free.

    THE ENVELOPE IS LOGGED, NEVER CARRIED INTO THE REFUSAL. `InboxUnreadable`'s message becomes the
    `detail` of a client-facing problem+json, and what arrives here is daprd's INTERNAL envelope
    wrapped in the SDK's own message — the actor error code, the pod's own address, the state
    component's name, and whatever else the runtime chose to embed. Re-raising with a fixed reason and
    putting the envelope on an ERROR line keeps the 503 diagnosable by an operator without publishing
    the inside of the cluster to a browser. It is also why the envelope is not parsed back into fields:
    a regex over somebody else's format would be a second thing to keep in step with the SDK, and
    nothing on the wire needs it.
    """

    async def invoke(*args: object, **kwargs: object) -> dict[str, Any]:
        try:
            return await call(*args, **kwargs)
        except Exception as exc:
            if InboxUnreadable.__name__ not in str(exc):
                raise
            logger.error("inbox_actor_refusal", extra={"envelope": str(exc)})
            raise InboxUnreadable("actor", "the actor refused to serve this inbox record") from exc

    return invoke


def typed_proxy(actor_type: str, actor_id: str, interface: type) -> TypedActorProxy:
    """A `TypedActorProxy` over a fresh sidecar channel — the lazy-import pattern every call site uses
    (`ActorProxy` opens a channel, so importing it at module scope would make merely importing a route
    module require daprd)."""
    from dapr.actor import ActorId, ActorProxy

    # `cast`, not a signature change: callers hand the concrete interface class, and narrowing the
    # parameter to `type[ActorInterface]` would force every unit-test fake to inherit dapr's base.
    return TypedActorProxy(ActorProxy.create(actor_type, ActorId(actor_id), cast("type[ActorInterface]", interface)), interface)


def inbox_for(subject: str) -> TypedActorProxy:
    """A proxy to ONE subject's inbox — the only way this service addresses an inbox.

    `subject` is the VERIFIED token sub and nothing else. There is deliberately no overload taking an
    actor id: a caller that could hand an id could hand somebody else's.
    """
    return typed_proxy(INBOX_ACTOR_TYPE, inbox_actor_id(subject), InboxActorInterface)
