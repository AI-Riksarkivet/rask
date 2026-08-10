"""The inbox's pure rules — order, page, count, compact — with the boundaries named.

These need no Dapr, no sidecar and no state manager, which is exactly why the rules live apart from
the actor: the cases that actually bite a feed (empty, exactly `limit`, one over) are decidable here.
"""

from datetime import UTC, datetime, timedelta

import pytest

from notifications.feed import compact, order_newest_first, paginate, unread_count, visible
from notifications.models import InboxCursor, InboxFilter, InboxPointer, InboxQuery, NotificationReason


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _pointer(minutes_ago: int, *, seen: bool = False, dismissed: bool = False, run: str | None = None) -> InboxPointer:
    run_id = run or f"run-{minutes_ago:03d}"
    return InboxPointer(
        notification_id=f"{run_id}@FAIL",
        reason=NotificationReason.AUTHOR,
        object_id="silver/pages",
        source_run_id=run_id,
        occurred_at=NOW - timedelta(minutes=minutes_ago),
        seen=seen,
        dismissed=dismissed,
    )


def _query(limit: int, *, state: InboxFilter = InboxFilter.ALL, after: InboxCursor | None = None) -> InboxQuery:
    return InboxQuery(limit=limit, state=state, after=after)


def test_the_order_is_newest_first_with_the_id_breaking_a_tie() -> None:
    same_instant = [_pointer(5, run="run-b"), _pointer(5, run="run-a")]
    ordered = order_newest_first([_pointer(10), *same_instant])
    assert [p.notification_id for p in ordered] == ["run-b@FAIL", "run-a@FAIL", "run-010@FAIL"]


def test_dismissed_rows_are_invisible_under_both_filters() -> None:
    """Dismissal is the reader saying "not this one", and the shared component drops dismissed rows
    before it counts anything. A backend that kept returning them would make the panel and the badge
    disagree about the same inbox."""
    rows = [_pointer(1), _pointer(2, seen=True), _pointer(3, dismissed=True), _pointer(4, seen=True, dismissed=True)]
    assert [p.notification_id for p in visible(rows, InboxFilter.ALL)] == ["run-001@FAIL", "run-002@FAIL"]
    assert [p.notification_id for p in visible(rows, InboxFilter.UNREAD)] == ["run-001@FAIL"]


def test_an_empty_inbox_pages_as_empty() -> None:
    page = paginate([], _query(10))
    assert (page.pointers, page.has_more, page.unread) == ([], False, 0)


def test_exactly_limit_rows_reports_no_more() -> None:
    page = paginate([_pointer(n) for n in range(1, 4)], _query(3))
    assert (len(page.pointers), page.has_more) == (3, False)


def test_one_row_over_the_limit_reports_more() -> None:
    page = paginate([_pointer(n) for n in range(1, 5)], _query(3))
    assert (len(page.pointers), page.has_more) == (3, True)


def test_a_cursor_resumes_strictly_after_the_row_it_names() -> None:
    rows = [_pointer(n) for n in range(1, 5)]
    first = paginate(rows, _query(2))
    last = first.pointers[-1]
    second = paginate(rows, _query(2, after=InboxCursor(occurred_at=last.occurred_at, notification_id=last.notification_id)))
    assert [p.notification_id for p in second.pointers] == ["run-003@FAIL", "run-004@FAIL"]
    assert second.has_more is False


def test_the_badge_counts_the_whole_inbox_not_the_page() -> None:
    """Counting over the page would make the badge shrink as the reader scrolls."""
    page = paginate([_pointer(n) for n in range(1, 6)], _query(2))
    assert (len(page.pointers), page.unread) == (2, 5)


def test_unread_is_neither_seen_nor_dismissed() -> None:
    assert unread_count([_pointer(1), _pointer(2, seen=True), _pointer(3, dismissed=True)]) == 1


def test_compaction_drops_rows_past_the_retention_window() -> None:
    """The pointer TTL math. Exactly-at-the-horizon is KEPT: the window is inclusive, so a row cannot
    be dropped by the same tick that first makes it eligible."""
    ttl = timedelta(hours=1)
    rows = [_pointer(59), _pointer(60), _pointer(61)]
    kept = compact(rows, now=NOW, ttl=ttl, max_rows=100)
    assert [p.notification_id for p in kept] == ["run-059@FAIL", "run-060@FAIL"]


def test_compaction_caps_the_row_count() -> None:
    kept = compact([_pointer(n) for n in range(1, 10)], now=NOW, ttl=timedelta(days=1), max_rows=3)
    assert len(kept) == 3


def test_the_cap_drops_handled_rows_before_unread_ones() -> None:
    """Truncating by age alone would drop an unread FAILED run to keep a dismissed one — the exact
    outcome this plane exists to end."""
    rows = [_pointer(1, seen=True), _pointer(2, dismissed=True), _pointer(30), _pointer(40)]
    kept = compact(rows, now=NOW, ttl=timedelta(days=1), max_rows=2)
    assert [p.notification_id for p in kept] == ["run-030@FAIL", "run-040@FAIL"]


def test_the_cap_still_bounds_an_inbox_that_is_all_unread() -> None:
    """A bound is a bound: an inbox nobody reads must not be able to outgrow it."""
    kept = compact([_pointer(n) for n in range(1, 10)], now=NOW, ttl=timedelta(days=1), max_rows=4)
    assert len(kept) == 4
    assert all(p.unread for p in kept)


@pytest.mark.parametrize("max_rows", [1, 5, 50])
def test_compaction_never_returns_more_than_the_cap(max_rows: int) -> None:
    rows = [_pointer(n, seen=n % 2 == 0) for n in range(1, 20)]
    assert len(compact(rows, now=NOW, ttl=timedelta(days=1), max_rows=max_rows)) <= max_rows
