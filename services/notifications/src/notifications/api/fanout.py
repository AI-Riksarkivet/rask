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
from collections.abc import Callable, Sequence

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict

from notifications.api.lineage_events import Notifiable
from notifications.api.metrics import Outcome, record_recipient
from notifications.api.visibility import Visibility
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


def audience_for(notice: Notifiable) -> tuple[str, ...]:
    """Who is told about this run. v1: its verified author, and nobody else.

    The one targeting source that needs no registry and no new FGA type — you may always be told about
    your own run. Project watches (gated on `project#member`) and governance events naming a subject
    join here, as additional sources over the same delivery path; the estate's standing rule is that
    membership gates watching and never implies it, so no audience widens by default.
    """
    return (notice.author,)


async def fan_out(
    notice: Notifiable,
    *,
    audience: Sequence[str],
    visibility: Visibility,
    open_inbox: InboxOpener,
) -> FanoutResult:
    """Write one pointer into each visible recipient's inbox; count everything else."""
    counts = dict.fromkeys((Outcome.DELIVERED, Outcome.DUPLICATE, Outcome.HIDDEN, Outcome.RETRIED), 0)
    payload = notice.delivery.model_dump(mode="json")
    with _tracer.start_as_current_span("inbox.fanout") as span:
        span.set_attribute("lance.notifications.recipients", len(audience))
        span.set_attribute("lance.notifications.notification_id", notice.delivery.notification_id)
        for subject in audience:
            outcome = await _deliver_one(subject, notice=notice, payload=payload, visibility=visibility, open_inbox=open_inbox)
            counts[outcome] += 1
            record_recipient(outcome)
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
