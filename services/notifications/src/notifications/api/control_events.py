"""v3 targeting — governance events that NAME a subject.

The third and last targeting source, and the only one where being told requires no relationship at
all: **being named IS the targeting.** A `grant_added` carries the subject it granted to; that person
is entitled to know their access changed, and no visibility check can express that — the grant may be
the very thing that makes an object visible, so checking visibility first would swallow exactly the
notification worth sending. A `grant_revoked` is the mirror and the sharper case: after a revoke the
subject can no longer see the object, so a delivery-time visibility check would drop the one event
they most need.

That is why this lane does not go through `fan_out`'s visibility gate. It is not an exception carved
out of the rule — it is a different question. The lineage lane asks "may this person be told about
this OBJECT"; this lane asks "was this person the SUBJECT of this act", and the event itself answers
it. Nothing about the object is disclosed beyond its id, which the subject was already named against.

**An action in `ERASING_ACTIONS` is the one exception that tells nobody:** it names a person the estate admin
offboarded, and the lane erases their inbox and watch enrolment instead (`erase_subject`, CTL-022).

**Only the actions in `NAMED_ACTIONS`, and only when `extra.subject` is present.** Every other control
action is a catalog mutation with no named party, and delivering those would recreate the estate-wide
feed this plane exists to replace. (This read "only the two grant actions" until 2026-08-16, by which
time the set held six — the drift was harmless here only because the constant, not the prose, is what
the gate reads.)
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from notifications.api.fanout import InboxOpener
from notifications.api.metrics import Lane, Outcome, record_ingress
from notifications.config import get_notifications_settings
from notifications.errors import InboxUnreadable
from notifications.models import NotificationDelivery, NotificationReason
from notifications.proxies import TypedActorProxy
from service_kit.control_events import CatalogControlEvent
from service_kit.governed import fga
from service_kit.lakehouse.subjects import is_person_subject


if TYPE_CHECKING:  # no runtime cost, and it breaks no import cycle
    from service_kit.governed.decider import AccessDecider


log = logging.getLogger(__name__)

#: The control actions that name a party. `grant_revoked` matters MORE than `grant_added`: losing
#: access silently is how someone discovers it by hitting a 403 in the middle of work.
#:
#: Every one is a change to the lakehouse's own state: a tool's work queue is the tool's to announce, on its own
#: channel (ADR 0171). Adding a member here is only ONE THIRD of the change: `ControlAction` must carry it (or the
#: envelope will not validate) and `NotificationReason` must too, because `as_delivery` builds
#: `NotificationReason(event.action)` and would raise on every delivery.
NAMED_ACTIONS: frozenset[str] = frozenset({"grant_added", "grant_revoked", "erasure_release_scheduled"})

#: The control actions that name a person and tell them nothing: the plane ERASES what it holds for them instead.
#: `principal_deleted` is the estate admin offboarding a principal ([[LH-233]]), and nobody is left to tell (CTL-022).
#: No `NotificationReason` matches, because no row is ever written for one. Verified like `NAMED_ACTIONS` at the
#: signature door: an erasure is the most destructive thing an event can make this plane do.
ERASING_ACTIONS: frozenset[str] = frozenset({"principal_deleted"})

#: How the lane addresses one project's `WatchIndexActor`. A callable for the reason `InboxOpener` is one.
type WatchIndexOpener = Callable[[str], TypedActorProxy]

#: How a USERSET (`role:reviewers#assignee`, `team:eng#member`) resolves to the people in it.
#:
#: A callable rather than an FGA import, for the reason every seam in this plane is one: the lane stays
#: exercisable with no FGA behind it, and a deployment with authorization off supplies nothing rather
#: than a stub that invents an audience. `None` means "cannot expand", which resolves to NO audience —
#: quiet, never wrong.
type UsersetExpander = Callable[[str], Awaitable[tuple[str, ...]]]


def userset_expander(decider: "AccessDecider | None") -> UsersetExpander | None:
    """Resolve `role:reviewers#assignee` to the people holding that relation, via the SAME
    `list_users` primitive the access review uses.

    `None` when authorization is off or unwired — the lane then names nobody for a userset rather
    than inventing an audience. Raises on an FGA outage rather than answering empty: the caller
    turns that into a RETRY, because an empty answer would read as "this group has no members" and
    silently drop everyone's notification.
    """
    # `get_notifications_settings()`: the FGA flag lives on the service settings, and `IngressSettings`
    # carries none, so reading it there raises AttributeError on the first control event naming a userset.
    if decider is None or not get_notifications_settings().fga_enabled:
        return None

    async def expand(userset: str) -> tuple[str, ...]:
        obj, _, relation = userset.partition("#")
        if not obj or not relation:
            return ()
        return tuple(await decider.list_users(relation=relation, obj=obj))

    return expand


def _is_userset(principal: str) -> bool:
    """`type:id#relation` — a GROUP, not a person. `model.fga` permits these on nearly every grantable
    relation (`role#assignee` on owner/writer/reader/validator/manage_grants, `team#member` on a
    project), so they are the estate's ordinary way to grant, not an edge case."""
    return "#" in principal


async def named_subjects(event: CatalogControlEvent, expand: UsersetExpander | None = None) -> tuple[str, ...]:
    """Every PERSON this governance event is about — empty when it names nobody.

    Plural because a grant may name a GROUP. `role:reviewers#assignee` is one principal and many
    people, and delivering to the principal itself created an InboxActor keyed
    `role:reviewers#assignee` that no human can open — unreadable state accumulating on every userset
    grant while telling none of the people it affected.

    Refusing usersets outright would have been the safe-looking fix and the wrong one: roles are the
    estate's primary grouping mechanism, so refusing means the commonest way to grant access notifies
    nobody. They are EXPANDED instead, through the same `list_users` primitive the access review uses.

    Expansion is best-effort in one direction only: with no expander wired the audience is EMPTY
    rather than the group string, because a phantom address is worse than silence. An expander that
    RAISES is left to propagate — the caller turns it into a RETRY, since an FGA outage is transient
    and dropping the event would lose the notification permanently.

    Read from `extra.subject` — the catalog stamps it on the grant actions and nothing else. Tolerant
    of an absent or oddly-typed value because `extra` is an open bag on an envelope this service does
    not own: a producer that stops stamping it makes this lane quiet, never wrong.

    An address must identify a person (`is_person_subject`), and that rule is applied HERE, at the one place that
    turns a principal into an inbox address, rather than at each producer: managed access stamps the wildcard
    `user:*` on an ordinary `grant_added`, and a grant may name a service account, and neither is a person's inbox.
    """
    if event.action not in NAMED_ACTIONS:
        return ()
    raw = event.extra.get("subject")
    if not isinstance(raw, str):
        return ()
    if _is_userset(raw.strip()):
        return () if expand is None else tuple(dict.fromkeys(s for s in await expand(raw.strip()) if is_person_subject(s)))
    # Every producer stamps the OpenFGA user the grant named (`fga.user_object`, `user:` and the principal key
    # percent-encoded), and an inbox is addressed by the principal key itself, so the id is decoded here
    # (`fga.subject_of`): the one translation between the two vocabularies, done HERE rather than in the actor so
    # the actor never learns about FGA at all. A key carrying an encoded character would otherwise address an inbox
    # nobody's token reaches ([[LH-063]]).
    #
    # The person rule is applied AFTER decoding, not before: a bare `"user:"` is truthy and decodes to nothing,
    # and a wildcard or a service account's principal addresses no person, so each is quiet here rather than an
    # inbox actor nobody opens, or an empty subject that `inbox_actor_id` raises on, turning a malformed producer
    # field into a RETRY loop on an event that can never succeed.
    person = _person(raw)
    return (person,) if person is not None else ()


async def _watched_projects(inbox: TypedActorProxy) -> list[str]:
    """The projects the person's watch list names; none when the list is unreadable, so the erasure still removes it.

    A list this build cannot parse, or one naming another subject, names no project this lane can trust, and refusing
    on it would RETRY the same refusal on every redelivery while the whole inbox stayed. The index enrolments it named
    are logged as left, the one residue the erasure cannot reach.
    """
    try:
        return [str(p) for p in (await inbox.get_watches()).get("projects") or []]
    except InboxUnreadable:
        log.warning("control_erasure_watches_unreadable")
        return []


def _person(raw: str) -> str | None:
    """The inbox a stamped plain subject addresses, or None when it addresses no person (a userset is not handled here)."""
    stripped = raw.strip()
    subject = fga.subject_of(stripped.removeprefix("user:")) if stripped.startswith("user:") else stripped
    return subject if is_person_subject(subject) else None


async def erase_subject(event: CatalogControlEvent, *, open_inbox: InboxOpener, open_watch_index: WatchIndexOpener, lane: Lane = Lane.BUS) -> dict[str, str]:
    """Erase everything this plane holds for the principal an `ERASING_ACTIONS` event names (CTL-022).

    The person's own watch list names the projects whose index enrols them, so each index is left first and the inbox
    erased last: a failure between the two leaves the list to find them by on the redelivery, and every step is
    idempotent, so a redelivered or replayed event converges on the same empty state and re-enrols nobody. A userset,
    a wildcard or a service account names no person whose state could be erased, so it erases nothing.
    """
    raw = event.extra.get("subject")
    subject = _person(raw) if isinstance(raw, str) and not _is_userset(raw.strip()) else None
    if subject is None:
        record_ingress(lane, Outcome.IGNORED)
        return {"status": "SUCCESS"}
    inbox = open_inbox(subject)
    try:
        watched = await _watched_projects(inbox)
        for project_id in watched:
            await open_watch_index(project_id).unwatch({"subject": subject})
        await inbox.erase()
    except Exception:
        # RETRY: the actor plane was unreachable or refused, and an erasure the redelivery can finish must not be acked.
        log.exception("control_erasure_failed", extra={"action": event.action})
        record_ingress(lane, Outcome.RETRIED)
        return {"status": "RETRY"}
    log.info("principal_erased", extra={"action": event.action, "watches_left": len(watched)})
    record_ingress(lane, Outcome.ERASED)
    return {"status": "SUCCESS"}


def as_delivery(event: CatalogControlEvent) -> NotificationDelivery:
    """Project one governance event onto a pointer.

    `notification_id` is `<event_id>@<ACTION>`, not `run_id@STATE`: a governance event has no run, and
    reusing the run scheme would let a grant collide with a run that happened to share an id. The
    `@ACTION` suffix keeps the estate's one property — dismissing a grant does not dismiss the revoke
    that follows it.
    """
    return NotificationDelivery(
        notification_id=f"{event.event_id}@{event.action.upper()}",
        reason=NotificationReason(event.action),
        object_id=event.object_id,
        object_name=event.object_name,
        object_uuid=event.object_uuid,
        occurred_at=event.occurred_at,
    )


#: How many members' inboxes are written at once, and how long the whole set may take. The deadline sits under the
#: tighter of the control component's two windows: 30 s of ackWait with resiliency off, and with it on 720 s less the
#: sidecar's four 120 s backoffs, shared by five attempts (48 s each; `chart/templates/dapr-component.yaml`,
#: `dapr-resiliency.yaml`). The handler's own time counts against the window, and one that outlasts it is redelivered
#: underneath itself.
_CONCURRENT_DELIVERIES = 8
_DELIVERY_DEADLINE_SECONDS = 20.0


async def _deliver_to_all(subjects: tuple[str, ...], payload: dict[str, object], *, open_inbox: InboxOpener, action: str) -> bool:
    """Write ``payload`` into every subject's inbox, at most `_CONCURRENT_DELIVERIES` at once; whether every one landed.

    Each member is attempted whatever another's inbox does, so one that fails or hangs neither stops nor delays the
    rest past the deadline; the deadline cancels what is still in flight and answers that not all landed.
    """
    gate = asyncio.Semaphore(_CONCURRENT_DELIVERIES)
    failed: list[str] = []

    async def deliver(subject: str) -> None:
        async with gate:
            try:
                await open_inbox(subject).deliver(payload)
            except Exception:
                # The sidecar owns the backoff. Unlike the watcher lookup, redelivery genuinely helps here: the failure
                # is the inbox actor being momentarily unreachable, not a missing audience.
                log.exception("control_notification_failed", extra={"action": action, "subject": subject})
                failed.append(subject)

    try:
        async with asyncio.timeout(_DELIVERY_DEADLINE_SECONDS), asyncio.TaskGroup() as group:
            for subject in subjects:
                group.create_task(deliver(subject))
    except TimeoutError:
        log.warning("control_notification_deadline", extra={"action": action, "members": len(subjects)})
        return False
    return not failed


async def ingest_control_event(
    raw: object,
    *,
    open_inbox: InboxOpener,
    open_watch_index: WatchIndexOpener,
    expand: UsersetExpander | None = None,
    lane: Lane = Lane.BUS,
) -> dict[str, str]:
    """Ingest one Dapr-delivered control event. Same DROP/RETRY/SUCCESS discipline as the run lane.

    An event naming nobody is a SUCCESS, not a DROP: it arrived intact and this plane simply has no
    audience for it. DROP is reserved for a payload that will not parse, where redelivery cannot help.
    """
    try:
        event = CatalogControlEvent.model_validate(raw)
    except Exception:
        log.error("control_event_invalid", extra={"lane": lane.value})
        record_ingress(lane, Outcome.DROPPED)
        return {"status": "DROP"}

    if event.action in ERASING_ACTIONS:
        return await erase_subject(event, open_inbox=open_inbox, open_watch_index=open_watch_index, lane=lane)

    try:
        subjects = await named_subjects(event, expand)
    except Exception:
        # An expander failure is an FGA outage, not an event this plane cannot target. RETRY rather
        # than IGNORE: the grant really did name people, and acking here would lose their notification
        # permanently — the same fail-closed reasoning the visibility gate uses on the run lane.
        log.exception("control_userset_expansion_failed", extra={"action": event.action})
        record_ingress(lane, Outcome.RETRIED)
        return {"status": "RETRY"}

    if not subjects:
        record_ingress(lane, Outcome.IGNORED)
        return {"status": "SUCCESS"}

    # ONE ROW PER PERSON, EVERY PERSON ATTEMPTED, and any failure retries the whole set (CTL-027): a loop that stopped at
    # the first failure would be redelivered in the same order and never reach the members after a member whose inbox
    # fails for good. Retrying the set is safe because delivery is idempotent on `notification_id` (`<event_id>@<ACTION>`,
    # stable across redelivery), so a member who already has the row takes it as a duplicate.
    if not await _deliver_to_all(subjects, as_delivery(event).model_dump(mode="json"), open_inbox=open_inbox, action=event.action):
        record_ingress(lane, Outcome.RETRIED)
        return {"status": "RETRY"}

    record_ingress(lane, Outcome.DELIVERED)
    return {"status": "SUCCESS"}
