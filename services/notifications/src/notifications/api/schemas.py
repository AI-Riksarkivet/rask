"""What the inbox door answers with.

Separate from `notifications.models` because these are the WIRE shapes, and the difference is
load-bearing rather than tidy: a route's return type is the serialization filter, so a response model
can only ever carry the fields it declares. `InboxMeta.rows` — how many records this subject's state
partition holds — is a storage fact the actor needs and a reader has no business knowing, and the
only reason it never reaches a browser is that no model here declares it.

`InboxPointer` was returned unchanged while it held nothing but the claim-check record. S5 gave it a
`sent` ledger — which channels this notification has already been pushed to — and that is DELIVERY
BOOKKEEPING, not something a reader has any business seeing: it discloses that a subject has email or
Slack wired, and to a reader of a shared screen it discloses it about someone else. So the wire row is
now a declared projection rather than the storage record, which is what this module said all along
about `InboxMeta.rows` and now has a second reason to say.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from notifications.models import InboxPointer


class InboxRow(BaseModel):
    """One inbox row AS THE WIRE SEES IT — the storage record minus its delivery bookkeeping.

    Every field here is one a reader can already ask for. `sent` is deliberately absent; see the
    module docstring. Built with an explicit field list rather than an exclusion, because an exclusion
    silently admits whatever the storage model grows next.
    """

    model_config = ConfigDict(frozen=True)

    notification_id: str
    reason: str
    object_id: str
    source_run_id: str | None = None
    event_seq: int | None = None
    occurred_at: datetime
    seen: bool = False
    dismissed: bool = False

    @classmethod
    def of(cls, pointer: InboxPointer) -> "InboxRow":
        return cls(
            notification_id=pointer.notification_id,
            reason=str(pointer.reason),
            object_id=pointer.object_id,
            source_run_id=pointer.source_run_id,
            event_seq=pointer.event_seq,
            occurred_at=pointer.occurred_at,
            seen=pointer.seen,
            dismissed=pointer.dismissed,
        )


class InboxFeed(BaseModel):
    """One page of a subject's inbox, plus the badge for the whole of it.

    `unread` counts the INBOX, not the page: the badge is a property of the inbox, and deriving it
    from a page would make it shrink as the reader scrolls. `next_cursor` is absent exactly when the
    server knows there is nothing after this page — a client stops on `null`, never on a short page.
    """

    model_config = ConfigDict(frozen=True)

    notifications: list[InboxRow]
    next_cursor: str | None = None
    unread: int = Field(ge=0)


class UnreadBadge(BaseModel):
    """The badge alone. Its own route because it is by far the most frequent read in the plane and it
    must not load the pointer records — which is the entire reason the actor splits its state into a
    small partition and a large one."""

    model_config = ConfigDict(frozen=True)

    unread: int = Field(ge=0)


class MarkResult(BaseModel):
    """How many rows this call actually changed, and the badge after it.

    `updated` is a count of CHANGES, not of ids handed in: marking an already-read row is a no-op, and
    reporting it as an update would make a client that re-sends its visible set think something moved.
    """

    model_config = ConfigDict(frozen=True)

    updated: int = Field(ge=0)
    unread: int = Field(ge=0)


class DismissResult(BaseModel):
    """Whether the dismissal changed anything, and the badge after it."""

    model_config = ConfigDict(frozen=True)

    dismissed: int = Field(ge=0)
    unread: int = Field(ge=0)
