"""The inbox's pure rules — order, page, count, compact — with the boundaries named.

These need no Dapr, no sidecar and no state manager, which is exactly why the rules live apart from
the actor: the cases that actually bite a feed (empty, exactly `limit`, one over) are decidable here.
"""

from datetime import UTC, datetime, timedelta

from notifications.feed import compact
from notifications.models import InboxPointer, NotificationReason


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


def test_compaction_drops_rows_past_the_retention_window() -> None:
    """The pointer TTL math. Exactly-at-the-horizon is KEPT: the window is inclusive, so a row cannot
    be dropped by the same tick that first makes it eligible."""
    ttl = timedelta(hours=1)
    rows = [_pointer(59), _pointer(60), _pointer(61)]
    kept = compact(rows, now=NOW, ttl=ttl, max_rows=100)
    assert [p.notification_id for p in kept] == ["run-059@FAIL", "run-060@FAIL"]


def test_the_cap_drops_handled_rows_before_unread_ones() -> None:
    """Truncating by age alone would drop an unread FAILED run to keep a dismissed one — the exact
    outcome this plane exists to end."""
    rows = [_pointer(1, seen=True), _pointer(2, dismissed=True), _pointer(30), _pointer(40)]
    kept = compact(rows, now=NOW, ttl=timedelta(days=1), max_rows=2)
    assert [p.notification_id for p in kept] == ["run-030@FAIL", "run-040@FAIL"]
