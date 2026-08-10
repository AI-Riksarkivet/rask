"""The shapes that cross the actor boundary, and the one id this plane does not get to choose."""

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from notifications.models import InboxPointer, NotificationDelivery, NotificationReason, notification_id


ROOT = Path(__file__).resolve().parents[3]


def _delivery(**kw: object) -> NotificationDelivery:
    base: dict[str, object] = {
        "notification_id": notification_id("run-1", "FAIL"),
        "reason": NotificationReason.AUTHOR,
        "object_id": "silver/pages",
        "source_run_id": "run-1",
        "occurred_at": datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
    }
    return NotificationDelivery.model_validate(base | kw)


def test_the_notification_id_is_run_id_at_state() -> None:
    assert notification_id("abc", "fail") == "abc@FAIL"
    assert notification_id("abc", "COMPLETE") == "abc@COMPLETE"


def test_the_id_scheme_matches_the_shared_bell() -> None:
    """The component already keys seen/dismissed by `run_id@STATE`, which is what makes dismissing a
    run's "started" still let its "failed" through. If the two ever disagree, every persisted read-state
    row points at a notification the panel cannot find — silently, because both sides still render."""
    source = (ROOT / "frontend/packages/ui/src/lib/runs/run-status.ts").read_text()
    assert re.search(r"run\.run_id\}@\$\{\(run\.state", source), "runNotificationId no longer builds `run_id@STATE`"


def test_a_delivery_cannot_state_read_state() -> None:
    """Read state is the subject's, minted by the actor. A replayed or forged delivery that could
    arrive pre-read would silently clear somebody's badge."""
    with pytest.raises(ValidationError):
        _delivery(seen=True)


def test_an_arriving_pointer_is_unread() -> None:
    pointer = InboxPointer.arriving(_delivery())
    assert (pointer.seen, pointer.dismissed, pointer.unread) == (False, False, True)


@pytest.mark.parametrize("mutate", ["marked_seen", "marked_dismissed"])
def test_a_read_row_is_not_unread(mutate: str) -> None:
    pointer = getattr(InboxPointer.arriving(_delivery()), mutate)()
    assert pointer.unread is False


def test_a_pointer_is_frozen() -> None:
    pointer = InboxPointer.arriving(_delivery())
    with pytest.raises(ValidationError):
        pointer.seen = True  # ty: ignore[invalid-assignment] — a frozen field; the raise IS the assertion


def test_instants_are_normalised_to_utc() -> None:
    """The feed's order is a `(occurred_at, notification_id)` comparison, and comparing an aware instant
    to a naive one RAISES — so one producer stamping a naive timestamp would take paging down for
    whoever received it, not merely mis-sort it."""
    naive = _delivery(occurred_at=datetime(2026, 8, 9, 12, 0))
    offset = _delivery(occurred_at=datetime(2026, 8, 9, 14, 0, tzinfo=timezone(timedelta(hours=2))))
    assert naive.occurred_at == datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    assert offset.occurred_at == datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
