"""The shapes that cross the actor boundary, and the one id this plane does not get to choose."""

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from notifications.models import InboxPointer, InboxRows, NotificationDelivery, NotificationReason, notification_id


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


class TestStoredRowsSurviveAVocabularyTheyPredate:
    """A STORED row must be readable by code that predates the vocabulary it was written with.

    THE OUTAGE THIS CLOSES, observed live. Three `NotificationReason` members were added
    (`originator`, `task_assigned`, `task_unassigned`) and rows carrying them landed in the durable
    actor state. The deployment was then rolled back to an image whose enum predated them, and the
    result was not a degraded row — it was `ValidationError: 4 validation errors for InboxRows`,
    surfaced as `InboxUnreadable` and a **503 for the entire inbox**. The bell fell back to the run
    feed, the badge went blank, and every OTHER notification that subject had was unreachable too.

    One unparseable row must never cost a subject their whole inbox. The asymmetry is the fix:
    `NotificationDelivery` is WIRE INPUT and stays strict — a forged delivery must still be refused at
    the door — while `InboxPointer` is state this service wrote itself and may be read back by an older
    build during any rollback or mixed-version rollout, which is a routine event and not a fault.
    """

    def _row(self, **over: object) -> dict[str, object]:
        row: dict[str, object] = {
            "notification_id": "run-1@FAIL",
            "reason": "author",
            "object_id": "silver$pages",
            "occurred_at": "2026-08-16T12:00:00Z",
        }
        row.update(over)
        return row

    def test_a_reason_from_a_newer_build_does_not_brick_the_inbox(self) -> None:
        rows = InboxRows.model_validate(
            {
                "subject": "alice",
                "updated_at": "2026-08-16T12:00:00Z",
                "pointers": [self._row(), self._row(notification_id="run-2@FAIL", reason="a_reason_invented_later")],
            }
        )
        assert len(rows.pointers) == 2, "the whole record must survive one row it cannot name"
        assert rows.pointers[0].reason is NotificationReason.AUTHOR, "known reasons are untouched"
        assert rows.pointers[1].reason is NotificationReason.UNKNOWN

    def test_an_unknown_FIELD_still_refuses_and_that_is_deliberate(self) -> None:
        """The tolerance stops at the reason value, and this pins the line.

        An unknown FIELD may be another subject's data — `extra="forbid"` on the stored row is a
        cross-subject containment guard (`test_inbox_leak_containment`), structural precisely so no
        later route can forget to filter. So drift THERE must keep reading as UNREADABLE rather than
        being quietly dropped, even though that is the same 503 the reason tolerance exists to avoid.
        The two are different hazards wearing one symptom: a foreign field crossing the actor boundary
        versus this subject's own row wearing a label this build cannot name."""
        with pytest.raises(ValidationError):
            InboxRows.model_validate(
                {
                    "subject": "alice",
                    "updated_at": "2026-08-16T12:00:00Z",
                    "pointers": [self._row(a_field_invented_later="x")],
                }
            )

    def test_the_DELIVERY_path_stays_strict(self) -> None:
        """The wire door does not relax. An ingress event naming a reason this build does not have is a
        producer/consumer mismatch to fix, not a row to tolerate — and `seen` must stay unspeakable."""
        with pytest.raises(ValidationError):
            NotificationDelivery.model_validate(self._row(reason="a_reason_invented_later"))
        with pytest.raises(ValidationError):
            NotificationDelivery.model_validate(self._row(seen=True))
