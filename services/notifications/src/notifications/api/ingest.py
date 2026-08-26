"""The ingress core — one lineage event in, one Dapr status out, shared by both lanes.

Everything a subscription handler must get right lives here rather than in the route, so it is
exercisable with no sidecar and so the RECONCILER runs the identical code path: the same event
arriving by bus and by feed goes through this function twice and lands exactly one pointer, because
the natural key it dedupes on is a property of the event rather than of the route it came in on.

**The status vocabulary, and why each one:**

* `DROP` — permanent. An unparseable payload cannot be fixed by redelivery, and a handler that RETRYs
  it (or raises) poisons the subscription: the same message comes back forever and everything queued
  behind it waits. Logged and counted, never silent.
* `RETRY` — transient. The actor plane or OpenFGA was unreachable. The SIDECAR owns the backoff and
  the eventual dead-letter park; there is deliberately no tenacity here, because a second retry layer
  multiplies the first (4 sidecar attempts x N app attempts) rather than adding resilience.
* `SUCCESS` — the ordinary ack, including for events this plane deliberately tells nobody about. A
  START event, an authorless run, a run with no outputs, and a run whose outputs the recipient may not
  see are all *handled*: there is nothing to redeliver.
"""

import logging
from typing import Final

from pydantic import ValidationError

from notifications.api.fanout import ChannelPush, FanoutResult, InboxOpener, WatcherLookup, audience_for, fan_out
from notifications.api.lineage_events import LineageRunEvent, notifiable
from notifications.api.metrics import Lane, Outcome, record_ingress
from notifications.api.visibility import Visibility


log = logging.getLogger(__name__)

#: The three answers the sidecar understands. Module constants rather than inline literals, matching
#: the lineage consumer — a typo in one of these strings is an ack the broker cannot interpret.
DAPR_SUCCESS: Final[dict[str, str]] = {"status": "SUCCESS"}
DAPR_RETRY: Final[dict[str, str]] = {"status": "RETRY"}
DAPR_DROP: Final[dict[str, str]] = {"status": "DROP"}


def _faults(exc: Exception) -> list[str]:
    """WHERE a payload failed to parse and HOW — with none of what it contained.

    `str(ValidationError)` renders `input_value=...` for every failing field, so logging it is logging
    the payload by another name. This topic is UNGOVERNED and multi-tenant: the fragment that ends up
    in this service's logs belongs to whoever published it, is never re-read through a governed path,
    and is bounded in size by pydantic's own repr truncation and by nothing else. The plane's whole
    premise is that it stores claim-checks and never content, and the DROP line was the one surface
    where that stopped being true.

    The field path plus the error type is what the line is actually read for — it names the producer's
    bug without quoting the producer's data. A non-pydantic fault carries no structure to report, so it
    reports its class.
    """
    if not isinstance(exc, ValidationError):
        return [type(exc).__name__]
    details = exc.errors(include_url=False, include_context=False, include_input=False)
    # `<root>` rather than an empty path: a whole-payload failure (a bare string, a list) has no `loc`,
    # and a fault that reads `": model_type"` is the one an operator skips over.
    return [f"{'.'.join(str(part) for part in detail['loc']) or '<root>'}: {detail['type']}" for detail in details]


def _event_outcome(result: FanoutResult) -> Outcome:
    """One label for an event whose recipients may have had different fates.

    Ordered by what an operator needs to see first: a failure outranks a delivery, a delivery outranks
    a duplicate, and `HIDDEN` is only reported when it is the whole story — an event delivered to one
    recipient and hidden from another is a DELIVERED event, not a half-hidden one.
    """
    if result.failed:
        return Outcome.RETRIED
    if result.delivered:
        return Outcome.DELIVERED
    if result.duplicate:
        return Outcome.DUPLICATE
    return Outcome.HIDDEN


async def ingest_run_event(
    raw: object,
    *,
    lane: Lane,
    visibility: Visibility,
    watchers: WatcherLookup | None = None,
    push: ChannelPush | None = None,
    open_inbox: InboxOpener,
    event_seq: int | None = None,
) -> dict[str, str]:
    """Project one OpenLineage run event onto its audience and return the sidecar's status.

    `raw` is the event itself, already unwrapped from whatever envelope carried it — the bus handler
    passes `body["data"]`, the reconciler passes the feed record's payload. Typed `object` because it
    is untrusted: everything it must be is decided by the validation on the next line.

    `event_seq` is the feed's sequence number and is stamped only on the lane that has one. It is
    written onto an already-validated delivery, so the copy needs no revalidation, and it is what
    makes an inbox row say which lane first told this subject about this run.
    """
    try:
        event = LineageRunEvent.model_validate(raw)
    except (ValidationError, TypeError, ValueError) as exc:
        # DROP, not RETRY: redelivery cannot make a malformed payload parse, and retrying it forever
        # is how a subscription stops delivering anything at all. Logged as faults rather than as the
        # rendered message — see `_faults`, which is where the claim-check invariant is kept.
        log.error("lineage_event_invalid", extra={"lane": lane.value, "faults": _faults(exc)})
        record_ingress(lane, Outcome.DROPPED)
        return DAPR_DROP

    notice = notifiable(event)
    if notice is None:
        record_ingress(lane, Outcome.IGNORED)
        return DAPR_SUCCESS
    if event_seq is not None:
        notice = notice.model_copy(update={"delivery": notice.delivery.model_copy(update={"event_seq": event_seq})})

    # The delivery-side `project#member` re-check rides the visibility client that is already here.
    # Wired at THIS seam rather than inside `audience_for` so the v1 audience — and every caller that
    # models no authorization — keeps working unchanged.
    audience = await audience_for(notice, watchers=watchers, members=visibility.still_a_member)
    result = await fan_out(notice, audience=audience, visibility=visibility, open_inbox=open_inbox, push=push)
    record_ingress(lane, _event_outcome(result))
    if result.needs_retry:
        return DAPR_RETRY
    return DAPR_SUCCESS
