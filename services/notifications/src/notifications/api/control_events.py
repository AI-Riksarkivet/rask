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

**Only the actions in `NAMED_ACTIONS`, and only when `extra.subject` is present.** Every other control
action is a catalog mutation with no named party, and delivering those would recreate the estate-wide
feed this plane exists to replace. (This read "only the two grant actions" until 2026-08-16, by which
time the set held six — the drift was harmless here only because the constant, not the prose, is what
the gate reads.)
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Final

from notifications.api.fanout import InboxOpener
from notifications.api.metrics import Lane, Outcome, record_ingress
from notifications.models import NotificationDelivery, NotificationReason
from service_kit.control_events import CatalogControlEvent


log = logging.getLogger(__name__)

#: The control actions that name a party. `grant_revoked` matters MORE than `grant_added`: losing
#: access silently is how someone discovers it by hitting a 403 in the middle of work.
#:
#: The task pair is the same shape one rung down — being handed work, and having it taken away — and the
#: annotator is the third producer on this topic. Adding a member here is only ONE THIRD of the change:
#: `ControlAction` must carry it (or the envelope will not validate) and `NotificationReason` must too,
#: because `as_delivery` builds `NotificationReason(event.action)` and would raise on every delivery.
NAMED_ACTIONS: frozenset[str] = frozenset(
    {"grant_added", "grant_revoked", "task_assigned", "task_unassigned", "task_changes_requested", "task_dropped", "task_lease_expired"}
)

#: The FGA wildcard principal, which is a grant to EVERYONE and therefore names no one.
#:
#: `POST .../managed-access` writes exactly this (`_MANAGED_ACCESS_SUBJECT`,
#: `catalog/api/v1/endpoints/access.py:455`) and emits a normal `grant_added`/`grant_revoked` carrying
#: it as `extra.subject`. It survived the emptiness check — `"user:*"` strips to a truthy `"*"` — so
#: every managed-access toggle delivered a pointer into an inbox actor literally named `*`.
#:
#: Filtered HERE, at the one place that turns a principal into an inbox address, rather than at the
#: catalog: this lane consumes an envelope it does not own, and the rule ("an address must identify a
#: person") belongs with the code that does the addressing. A producer stamping any other non-personal
#: principal is then quiet by the same rule instead of needing its own patch.
_WILDCARD: Final = "*"

#: How a USERSET (`role:reviewers#assignee`, `team:eng#member`) resolves to the people in it.
#:
#: A callable rather than an FGA import, for the reason every seam in this plane is one: the lane stays
#: exercisable with no FGA behind it, and a deployment with authorization off supplies nothing rather
#: than a stub that invents an audience. `None` means "cannot expand", which resolves to NO audience —
#: quiet, never wrong.
type UsersetExpander = Callable[[str], Awaitable[tuple[str, ...]]]


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
    """
    if event.action not in NAMED_ACTIONS:
        return ()
    raw = event.extra.get("subject")
    if not isinstance(raw, str):
        return ()
    if _is_userset(raw.strip()):
        return () if expand is None else tuple(dict.fromkeys(s for s in await expand(raw.strip()) if s and s != _WILDCARD))
    # The catalog writes FGA-style principals (`user:alice`); an inbox is addressed by the bare token
    # sub. Stripping the type prefix is the one translation between the two vocabularies, and it is
    # done HERE rather than in the actor so the actor never learns about FGA at all.
    #
    # Emptiness is checked AFTER normalizing, not before: a bare `"user:"` is truthy and strips to
    # nothing, so the pre-check let it through and handed `inbox_actor_id` an empty subject — which
    # raises, turning a malformed producer field into a RETRY loop on an event that can never succeed.
    subject = raw.removeprefix("user:").strip()
    if not subject or subject == _WILDCARD:
        return ()
    return (subject,)


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
        source_run_id=None,
        occurred_at=event.occurred_at,
    )


async def ingest_control_event(raw: object, *, open_inbox: InboxOpener, expand: UsersetExpander | None = None) -> dict[str, str]:
    """Ingest one Dapr-delivered control event. Same DROP/RETRY/SUCCESS discipline as the run lane.

    An event naming nobody is a SUCCESS, not a DROP: it arrived intact and this plane simply has no
    audience for it. DROP is reserved for a payload that will not parse, where redelivery cannot help.
    """
    try:
        event = CatalogControlEvent.model_validate(raw)
    except Exception:
        log.error("control_event_invalid", extra={"lane": Lane.BUS.value})
        record_ingress(Lane.BUS, Outcome.DROPPED)
        return {"status": "DROP"}

    try:
        subjects = await named_subjects(event, expand)
    except Exception:
        # An expander failure is an FGA outage, not an event this plane cannot target. RETRY rather
        # than IGNORE: the grant really did name people, and acking here would lose their notification
        # permanently — the same fail-closed reasoning the visibility gate uses on the run lane.
        log.exception("control_userset_expansion_failed", extra={"action": event.action})
        record_ingress(Lane.BUS, Outcome.RETRIED)
        return {"status": "RETRY"}

    if not subjects:
        record_ingress(Lane.BUS, Outcome.IGNORED)
        return {"status": "SUCCESS"}

    # ONE ROW PER PERSON, and a partial failure retries the whole set. Safe because delivery is
    # idempotent on `notification_id` (`<event_id>@<ACTION>`, stable across redelivery), so a member
    # who already has the row is a no-op on the retry — the same property the run lane's fan-out rests
    # on. A group grant must not be able to half-deliver and then stop.
    payload = as_delivery(event).model_dump(mode="json")
    try:
        for subject in subjects:
            await open_inbox(subject).deliver(payload)
    except Exception:
        # RETRY, and the sidecar owns the backoff. Unlike the watcher lookup, redelivery genuinely
        # helps here: the failure is the inbox actor being momentarily unreachable, not a missing
        # audience, and the delivery is idempotent on the notification id.
        log.exception("control_notification_failed", extra={"action": event.action})
        record_ingress(Lane.BUS, Outcome.RETRIED)
        return {"status": "RETRY"}

    record_ingress(Lane.BUS, Outcome.DELIVERED)
    return {"status": "SUCCESS"}
