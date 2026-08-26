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
from typing import TYPE_CHECKING, Any, Protocol, cast

from notifications.errors import InboxUnreadable
from notifications.inbox_actor import INBOX_ACTOR_TYPE, InboxActorInterface
from notifications.watch_actor import WATCH_ACTOR_TYPE, WatchIndexActorInterface
from service_kit.governed.user_state import DAPR_APP_ID_SEPARATOR, encode_subject


if TYPE_CHECKING:
    from dapr.actor import ActorInterface

    from notifications.api.channels import ChannelTable


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
        if wire is None:
            # PRESENT-and-None is not the same as absent, and only the second reaches the default
            # above. A decorator that recorded no wire name therefore handed `None` to `getattr`,
            # which raises "attribute name must be string" from inside the proxy — a TypeError about
            # strings, naming neither the interface nor the method, for what is a mis-declared actor
            # method. Refuse here, by name.
            raise AttributeError(f"{self._interface.__name__}.{name} is declared with no actor wire name")
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


class WatchIndexUnavailable(RuntimeError):
    """The watch index could not be READ — distinct from a project that has no watchers.

    Its own type because the two used to be one answer (`[]`), which made the bus lane decide a
    project had no watchers when what had actually happened was a sidecar restart.
    """


async def watchers_of(project_id: str) -> list[str]:
    """The subjects watching `project_id`.

    RAISES `WatchIndexUnavailable` when the index cannot be read, so the bus handler answers RETRY.
    This wrapper used to absorb every fault and return `[]`, on the stated ground that "a watcher-index
    outage does not heal on redelivery" — and for the faults it actually covers (a sidecar restart, an
    actor rebalance, a state-store failover) it heals in seconds. The retry is BOUNDED besides: the
    subscription registers a `deadLetterTopic`, so an event that keeps failing parks visibly instead
    of redelivering forever, and `audience_for` already documents a raising resolver as a supported
    shape. The cost of the retry is one counted duplicate for the author, because the fan-out is
    idempotent on the notification's natural key.

    A `ValueError` from `watch_index_for` is STILL absorbed, and the asymmetry is the point: an
    unusable project id is permanent, so answering RETRY would park the AUTHOR's own notification in
    the DLQ over a watcher lookup that can never succeed. Degrading to v1's audience is right there
    and wrong for a transient fault.
    """
    try:
        result = await watch_index_for(project_id).list_watchers()
    except ValueError:
        logger.exception("watch_index_unusable_project", extra={"project_id": project_id})
        return []
    except Exception as exc:
        logger.exception("watch_index_unreadable", extra={"project_id": project_id})
        raise WatchIndexUnavailable(project_id) from exc
    return [str(s) for s in (result.get("subjects") or [])]


@lru_cache(maxsize=1)
def _channel_table() -> "ChannelTable | None":
    """This deployment's channel senders, built ONCE.

    Split out from `channel_push` so the two pushers over it — the deferring one and the digest
    drain's — share a single `DaprClient`. `DaprClient()` opens a channel, so building it per pusher
    would open one per variant, and building it at import would make merely importing this module
    require a sidecar.

    `None` rather than an empty table, because the two say different things to the fan-out: an empty
    table still costs a prefs read per delivery to discover it can send nothing, while `None` skips
    the hop entirely. On an estate with channels off — the default — that is the whole cost removed
    rather than merely made cheap.
    """
    from notifications.api.channels import EMAIL, SLACK, ChannelTable, make_binding_sender, make_slack_sender
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
        # `make_slack_sender`, not the generic one: the webhook's payload contract (a JSON object,
        # sent as JSON) is the provider's, and a bare body was refused every time.
        table[SLACK] = make_slack_sender(client, binding=settings.slack_binding, timeout_seconds=settings.channel_timeout_seconds)
    return table or None


@lru_cache(maxsize=1)
def channel_push() -> "Callable[[str, dict[str, Any]], Awaitable[None]] | None":
    """This deployment's channel pusher, or `None` when no channel is enabled."""
    from notifications.api.channels import make_push

    table = _channel_table()
    return None if table is None else make_push(table, open_inbox=inbox_for)


class DigestInbox(Protocol):
    """What the digest pusher needs of an inbox, regardless of how it reaches one.

    A Protocol because there are genuinely two implementations and they differ in the one way that
    matters here: `TypedActorProxy` reaches the actor THROUGH the sidecar (a turn), while the actor
    passed to `digest_push_into` IS the actor (no turn). Naming the shape rather than the class is
    what lets the drain hand over `self` without either side importing the other.
    """

    async def get_prefs(self) -> dict[str, Any]: ...

    async def claim_channel(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def arm_digest(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def digest_push_into(inbox: DigestInbox) -> "Callable[[str, dict[str, Any]], Awaitable[None]] | None":
    """The DIGEST DRAIN's pusher, sending through the inbox object it is handed.

    Two differences from `channel_push`, and both are load-bearing.

    It does not DEFER. Handing the drain the deferring pusher is what made a digested notification
    unsendable -- every pointer it had just drained met the same conditions again and re-armed the
    very window it came from. See `make_push`.

    It does not OPEN A PROXY. The drain runs inside `InboxActor`'s own reminder turn, and daprd holds
    that actor's turn lock for the whole callback. `make_push`'s first act is `open_inbox(subject)`
    followed by `await inbox.get_prefs()`; with `inbox_for` that resolves to
    `InboxActor/<encode_subject(subject)>` -- the same type and the same id -- so the call blocked on
    the lock the callback already held, until DAPR_HTTP_TIMEOUT_SECONDS, and the exception was
    swallowed. Because `drain_digest` had already closed the window, every digested notification was
    drained and never sent, permanently. Passing the actor ITSELF makes `get_prefs` and
    `claim_channel` ordinary in-turn method calls with identical semantics and no sidecar hop.

    Deliberately NOT `lru_cache`d, unlike its sibling: the argument is a live actor instance, and
    caching on it would pin one subject's actor for the life of the process. The table underneath is
    cached, so the only per-call work is building the closure.
    """
    from notifications.api.channels import make_push

    table = _channel_table()
    return None if table is None else make_push(table, open_inbox=lambda _subject: inbox, defer=False)
