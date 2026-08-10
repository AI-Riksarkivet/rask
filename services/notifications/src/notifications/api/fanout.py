"""Delivering one notification to its audience — batch, with partial failure.

Two rules, and they are the whole module:

* **One bad recipient never aborts the audience.** A fan-out that stops at the first failure delivers
  a notification to some subset of the people who should have had it, and the subset depends on dict
  order. Each recipient is attempted, each failure is logged and counted, and the RESULT says how many
  failed so the caller can decide the sidecar's answer.
* **A retry is safe because delivery is idempotent.** RETRY re-drives the whole audience, including
  the recipients who already have the pointer — and that is a no-op, because the actor is idempotent
  on `notification_id`, which for a terminal event is lineage's own `(run_id, event_type)` natural
  key. Without that, "one bad recipient" would force a choice between losing deliveries and doubling
  them.

`except Exception` per recipient is the sanctioned broad catch: it logs and counts, never `pass`. It
covers the FGA outage too, deliberately — an outage is not "a bad recipient", but the caller's answer
is the same (RETRY), and routing it through the same counter keeps one path instead of two.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict

from notifications.api.lineage_events import Notifiable
from notifications.api.metrics import Outcome, record_recipient
from notifications.api.visibility import Visibility
from notifications.models import NotificationReason
from notifications.proxies import TypedActorProxy


log = logging.getLogger(__name__)
_tracer = trace.get_tracer("lance.notifications")


#: How the fan-out reaches one subject's inbox. A parameter rather than a direct `inbox_for` call so
#: the audience rules are exercisable with no sidecar — and so there is exactly one production value,
#: passed by the two ingress lanes.
type InboxOpener = Callable[[str], TypedActorProxy]


class FanoutResult(BaseModel):
    """What became of one notification across its audience. Counts only — a subject is per-user data
    and belongs on the log line, not in a value the caller might hand to a metric."""

    model_config = ConfigDict(frozen=True)

    delivered: int = 0
    #: Already in that inbox: an at-least-once redelivery, or the same run arriving on the other lane.
    duplicate: int = 0
    #: The recipient may not see every output, so they are not told.
    hidden: int = 0
    failed: int = 0

    @property
    def needs_retry(self) -> bool:
        """Any transient failure makes the WHOLE delivery retryable — safe because it is idempotent."""
        return self.failed > 0


#: How a caller resolves a project's watchers. A CALLABLE rather than a proxy import so the fan-out
#: stays testable without a sidecar — the same seam `InboxOpener` already is.
type WatcherLookup = Callable[[str], Awaitable[Sequence[str]]]

#: How a caller pushes one delivered pointer to a subject's opted-in channels. A CALLABLE for the same
#: reason `InboxOpener` is one: the fan-out stays testable with no Dapr client and no bindings, and a
#: deployment with channels off passes `None` rather than a table of senders that refuse.
type ChannelPush = Callable[[str, dict[str, object]], Awaitable[None]]


async def audience_for(notice: Notifiable, *, watchers: WatcherLookup | None = None) -> tuple[str, ...]:
    """Who is told about this run: its verified author (v1) UNION the project's watchers (v2).

    The author needs no registry and no permission — you may always be told about your own run.
    Watchers are an explicit, `project#member`-gated opt-in, and the estate's standing rule holds:
    membership GATES watching and never implies it, so no audience widens by default. Nothing here
    authorizes anything — every recipient's visibility is re-derived at delivery, which is what makes
    a stale watch harmless rather than a leak.

    ORDER IS AUTHOR-FIRST AND DEDUPED, so an author who also watches their own project is told once,
    and the one recipient guaranteed to exist is the one attempted first. `watchers=None` (and a
    project-less run) is v1's exact behaviour, which is what a lookup failure degrades to: a
    resolver that raises is a bus handler that RETRIES, so callers that cannot tolerate that pass a
    resolver which absorbs its own faults.
    """
    audience = [notice.author]
    if watchers is not None and notice.project:
        for subject in await watchers(notice.project):
            if subject not in audience:
                audience.append(subject)
    return tuple(audience)


async def fan_out(
    notice: Notifiable,
    *,
    audience: Sequence[str],
    visibility: Visibility,
    open_inbox: InboxOpener,
    push: ChannelPush | None = None,
) -> FanoutResult:
    """Write one pointer into each visible recipient's inbox; count everything else.

    `push` runs only for a recipient whose row was ACTUALLY WRITTEN — never for a duplicate, a hidden
    row or a failure. That ordering is the design: a channel push announces an inbox row, so pushing
    for a row that already existed is how a redelivery becomes a second email, and pushing for a
    hidden one would tell someone about a run the visibility gate just refused them.
    """
    counts = dict.fromkeys((Outcome.DELIVERED, Outcome.DUPLICATE, Outcome.HIDDEN, Outcome.RETRIED), 0)

    # Minted PER RECIPIENT, because the reason is a per-recipient fact: the same run reaches its
    # author because they ran it and a watcher because they asked to be told, and a row that recorded
    # only one of those could not be re-checked against the right rule later (the `NotificationReason`
    # docstring's own argument). Everything else in the delivery is identical, so this is one field.
    def payload_for(subject: str) -> dict[str, object]:
        reason = NotificationReason.AUTHOR if subject == notice.author else NotificationReason.WATCH
        return notice.delivery.model_copy(update={"reason": reason}).model_dump(mode="json")

    with _tracer.start_as_current_span("inbox.fanout") as span:
        span.set_attribute("lance.notifications.recipients", len(audience))
        span.set_attribute("lance.notifications.notification_id", notice.delivery.notification_id)
        for subject in audience:
            payload = payload_for(subject)
            outcome = await _deliver_one(subject, notice=notice, payload=payload, visibility=visibility, open_inbox=open_inbox)
            counts[outcome] += 1
            record_recipient(outcome)
            if outcome is Outcome.DELIVERED and push is not None:
                # Best-effort, and its faults never change the OUTCOME: the inbox row is written and
                # the bell is correct whatever the channel plane does. A push failure that flipped the
                # result to RETRY would redeliver the whole event to re-attempt an email — re-writing
                # nothing (the row is idempotent) to retry the one thing a person would see twice.
                try:
                    await push(subject, payload)
                except Exception:
                    log.exception("channel_push_failed", extra={"subject": subject})
    return FanoutResult(
        delivered=counts[Outcome.DELIVERED],
        duplicate=counts[Outcome.DUPLICATE],
        hidden=counts[Outcome.HIDDEN],
        failed=counts[Outcome.RETRIED],
    )


async def _deliver_one(
    subject: str,
    *,
    notice: Notifiable,
    payload: dict[str, object],
    visibility: Visibility,
    open_inbox: InboxOpener,
) -> Outcome:
    """One recipient's outcome. Never raises — the caller's whole contract is that it cannot."""
    try:
        if not await visibility.sees_all(subject, notice.outputs):
            return Outcome.HIDDEN
        result = await open_inbox(subject).deliver(payload)
    except Exception:
        # Inside the active span, and `log.exception` rather than `span.record_exception` (deprecated).
        # The subject rides the log line — where per-user data belongs — and never a metric label.
        log.exception(
            "notification_delivery_failed",
            extra={"subject": subject, "notification_id": notice.delivery.notification_id},
        )
        return Outcome.RETRIED
    return Outcome.DELIVERED if result.get("delivered") else Outcome.DUPLICATE
