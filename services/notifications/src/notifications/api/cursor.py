"""The inbox feed's opaque cursor — `(occurred_at, notification_id)`, base64url.

An inbox is a FEED: rows arrive at the top while a reader is paging, so an offset is wrong in the way
that matters — page 2 of an offset-paginated feed re-shows a row that moved down and skips one that
moved up. A keyset cursor over the feed's own total order has neither failure, costs no count query,
and resumes from a row rather than from a position.

**Both halves of the key, always.** `occurred_at` alone is not unique — two runs can finish in the
same millisecond — and a cursor over a non-unique key either repeats a row or skips one. The
`notification_id` tiebreaker is what makes the order total, so it travels in the cursor.

**Opaque on the wire, and that is a contract rather than obfuscation.** The client is told to hand the
string back, not to construct one: the pair inside is this service's ordering key, and a client that
learned to build one would pin the ordering forever. An unreadable cursor is a 400 naming itself —
never a silent restart from the top, which reads to a paging client as an infinite feed.
"""

import base64
import binascii

from pydantic import ValidationError

from notifications.models import InboxCursor, InboxPointer
from service_kit import exceptions


def encode_cursor(pointer: InboxPointer) -> str:
    """The cursor that resumes strictly after `pointer` — built from the LAST row a page served."""
    raw = InboxCursor(occurred_at=pointer.occurred_at, notification_id=pointer.notification_id).model_dump_json()
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(raw: str) -> InboxCursor:
    """Parse a cursor a client handed back.

    Raises:
        service_kit.exceptions.ValidationError: For anything that is not a cursor this service minted
            — 400 problem+json. Refusing is the honest answer: a client that mistyped a cursor and got
            page one back would page forever without noticing.
    """
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError) as exc:
        raise exceptions.ValidationError("the inbox cursor is not readable") from exc
    try:
        return InboxCursor.model_validate_json(decoded)
    except (ValidationError, UnicodeDecodeError) as exc:
        raise exceptions.ValidationError("the inbox cursor is not readable") from exc
