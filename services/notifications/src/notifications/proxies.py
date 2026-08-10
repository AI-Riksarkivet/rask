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
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

from notifications.errors import InboxUnreadable
from notifications.inbox_actor import INBOX_ACTOR_TYPE, InboxActorInterface
from notifications.watch_actor import WATCH_ACTOR_TYPE, WatchIndexActorInterface
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


def watch_index_for(project_id: str) -> TypedActorProxy:
    """A proxy to ONE project's watcher index.

    The actor id is the project id VERBATIM, not an encoded subject — a project id is estate-chosen
    and already a path-safe identifier, and encoding it would make the actor unreadable to an operator
    for no gain. The subject encoding exists because a `sub` is identity-provider-chosen and may carry
    Dapr's reserved `||`; a project id may not.

    Raises:
        ValueError: If `project_id` is empty, or could carry Dapr's reserved app-id separator — the
            same guard `inbox_actor_id` applies, for the same reason, on the one input that reaches
            the sidecar's URL path.
    """
    if not project_id.strip():
        raise ValueError("a watch index requires a non-empty project id")
    if DAPR_APP_ID_SEPARATOR in project_id:
        raise ValueError(f"project id {project_id!r} contains Dapr's reserved app-id separator {DAPR_APP_ID_SEPARATOR!r}")
    return typed_proxy(WATCH_ACTOR_TYPE, project_id, WatchIndexActorInterface)


async def watchers_of(project_id: str) -> list[str]:
    """The subjects watching `project_id`, or an EMPTY list if the index cannot be read.

    Absorbing its own faults is the whole point of this wrapper. `audience_for` is called from a bus
    handler, so a raising resolver is a handler that answers RETRY — and the sidecar would then
    redeliver a run whose AUTHOR was already notified, forever, because a watcher-index outage does
    not heal on redelivery. Degrading to v1's audience is the honest failure: the author is still
    told, the watchers are not, and the fact is on an ERROR line rather than in a redelivery loop.
    """
    try:
        result = await watch_index_for(project_id).list_watchers()
    except Exception:
        logger.exception("watch_index_unreadable", extra={"project_id": project_id})
        return []
    return [str(s) for s in (result.get("subjects") or [])]


@lru_cache(maxsize=1)
def channel_push() -> "Callable[[str, dict[str, Any]], Awaitable[None]] | None":
    """This deployment's channel pusher, or `None` when no channel is enabled.

    `None` rather than an empty table, because the two say different things to the fan-out: an empty
    table still costs a prefs read per delivery to discover it can send nothing, while `None` skips
    the hop entirely. On an estate with channels off — the default — that is the whole cost removed
    rather than merely made cheap.

    The Dapr client is built lazily and cached for the process: `DaprClient()` opens a channel, so
    constructing it at import would make merely importing this module require a sidecar.
    """
    from notifications.api.channels import EMAIL, SLACK, ChannelTable, make_binding_sender, make_push
    from notifications.api.settings import get_ingress_settings

    settings = get_ingress_settings()
    enabled = {name.strip() for name in settings.enabled_channels.split(",") if name.strip()}
    if not enabled:
        return None

    from dapr.aio.clients import DaprClient

    client = DaprClient()
    table: ChannelTable = {}
    if EMAIL in enabled:
        table[EMAIL] = make_binding_sender(client, binding=settings.email_binding, operation="create", timeout_seconds=settings.channel_timeout_seconds)
    if SLACK in enabled:
        table[SLACK] = make_binding_sender(client, binding=settings.slack_binding, operation="post", timeout_seconds=settings.channel_timeout_seconds)
    if not table:
        return None
    return make_push(table, open_inbox=inbox_for)
