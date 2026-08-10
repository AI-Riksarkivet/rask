"""The feed cursor — both halves of the key, opaque on the wire, and refused when it is not ours."""

from datetime import UTC, datetime, timedelta

import pytest

from notifications.api.cursor import decode_cursor, encode_cursor
from notifications.models import InboxPointer, NotificationReason
from service_kit import exceptions


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _pointer(*, notification_id: str = "run-1@FAIL", minutes_ago: int = 0) -> InboxPointer:
    return InboxPointer(
        notification_id=notification_id,
        reason=NotificationReason.AUTHOR,
        object_id="silver$pages",
        occurred_at=NOW - timedelta(minutes=minutes_ago),
    )


def test_a_cursor_round_trips_both_halves_of_the_ordering_key() -> None:
    """`occurred_at` alone is not unique — two runs can finish in the same millisecond — and a cursor
    over a non-unique key either repeats a row or skips one."""
    cursor = decode_cursor(encode_cursor(_pointer(notification_id="run-9@COMPLETE", minutes_ago=3)))
    assert cursor.notification_id == "run-9@COMPLETE"
    assert cursor.occurred_at == NOW - timedelta(minutes=3)


def test_two_rows_at_the_same_instant_produce_different_cursors() -> None:
    a = encode_cursor(_pointer(notification_id="run-a@FAIL"))
    b = encode_cursor(_pointer(notification_id="run-b@FAIL"))
    assert a != b


def test_a_cursor_carries_no_padding_so_it_survives_a_query_string() -> None:
    """base64url with `=` stripped: padding is the one character of the alphabet a URL re-encodes,
    and a cursor that changes shape between the response and the next request is not opaque, it is
    broken."""
    encoded = encode_cursor(_pointer())
    assert "=" not in encoded
    assert "+" not in encoded
    assert "/" not in encoded


@pytest.mark.parametrize("raw", ["not-a-cursor", "!!!!", "eyJhIjogMX0", "", "e30"])
def test_a_cursor_this_service_did_not_mint_is_refused(raw: str) -> None:
    """Never a silent restart from the top: a client that mistyped a cursor and got page one back
    would page forever without ever noticing."""
    with pytest.raises(exceptions.ValidationError):
        decode_cursor(raw)


def test_a_refused_cursor_is_a_400_rather_than_a_500() -> None:
    """`service_kit.exceptions.ValidationError` renders as RFC 9457 problem+json with a 400 — the
    caller's mistake, named, rather than an internal error."""
    with pytest.raises(exceptions.ValidationError) as caught:
        decode_cursor("garbage")
    assert caught.value.status_code == 400
