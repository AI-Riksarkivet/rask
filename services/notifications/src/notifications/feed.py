"""The inbox's pure rules: order, page, count, compact.

Everything here is a function over a list of pointers. It is separated from the actor deliberately —
the actor owns *when* the rules run and how the result is persisted; these are *what* the rules are,
and they need no Dapr, no sidecar and no state manager to be exercised. The boundary cases that
actually bite a feed (empty, exactly `limit`, one over) are decidable right here.
"""

from datetime import datetime, timedelta

from notifications.models import InboxFilter, InboxPage, InboxPointer, InboxQuery


def _order_key(pointer: InboxPointer) -> tuple[datetime, str]:
    """The total order: newest instant first, `notification_id` breaking a tie.

    The tiebreaker is not decoration — two runs can carry the same terminal instant, and a cursor over
    a non-unique key either repeats a row or skips one.
    """
    return (pointer.occurred_at, pointer.notification_id)


def order_newest_first(pointers: list[InboxPointer]) -> list[InboxPointer]:
    return sorted(pointers, key=_order_key, reverse=True)


def unread_count(pointers: list[InboxPointer]) -> int:
    """The badge, defined once. Every mutation re-derives it rather than adjusting a counter: a stored
    count and a stored row set are two truths that disagree the first time one write lands alone."""
    return sum(1 for pointer in pointers if pointer.unread)


def visible(pointers: list[InboxPointer], state: InboxFilter) -> list[InboxPointer]:
    """The rows a reader may be shown under `state`.

    Dismissed rows are excluded from BOTH filters — that is what dismissal means, and it is what the
    shared component already does (`visibleRuns` drops them before `unreadRuns` counts anything). A
    backend that kept returning them would make the panel and the badge disagree.
    """
    if state is InboxFilter.UNREAD:
        return [pointer for pointer in pointers if pointer.unread]
    return [pointer for pointer in pointers if not pointer.dismissed]


def paginate(pointers: list[InboxPointer], query: InboxQuery) -> InboxPage:
    """One page of the feed, resumed strictly after `query.after` in the feed's own order.

    `unread` is counted over the WHOLE inbox, not the page: the badge is a property of the inbox, and
    computing it from a page would make it shrink as the reader scrolls.
    """
    candidates = order_newest_first(visible(pointers, query.state))
    if query.after is not None:
        resume = (query.after.occurred_at, query.after.notification_id)
        candidates = [pointer for pointer in candidates if _order_key(pointer) < resume]
    # limit + 1 is the has-more probe: a feed has no total worth counting, and asking for one extra row
    # answers the only question a reader has.
    window = candidates[: query.limit + 1]
    return InboxPage(pointers=window[: query.limit], has_more=len(window) > query.limit, unread=unread_count(pointers))


def compact(pointers: list[InboxPointer], *, now: datetime, ttl: timedelta, max_rows: int) -> list[InboxPointer]:
    """Bound one inbox: drop what has aged out, then cap what is left.

    This is the ONLY thing that bounds an inbox. `ActorStateTTL` is not enabled on this estate's Dapr
    Configuration (open_notifications §11 q6, read from the chart), so the actor does not send a
    `ttlInSeconds` at all — daprd refuses the write when it does — and nothing here may assume a row
    disappears on its own.

    When the cap bites, rows the reader has already dealt with go first. Truncating by age alone would
    drop an unread FAILED run to keep a dismissed one, which is precisely the outcome the notification
    plane exists to end. An inbox whose unread rows alone exceed the cap is still truncated: the bound
    is a bound.
    """
    within = [pointer for pointer in pointers if pointer.occurred_at >= now - ttl]
    ordered = order_newest_first(within)
    if len(ordered) <= max_rows:
        return ordered
    kept = [pointer for pointer in ordered if pointer.unread][:max_rows]
    handled = [pointer for pointer in ordered if not pointer.unread]
    return order_newest_first(kept + handled[: max_rows - len(kept)])
