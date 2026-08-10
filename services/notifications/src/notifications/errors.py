"""The one domain error this plane owns.

`InboxUnreadable` exists to keep "there is nothing here" and "there is something here I refuse to
serve" apart. That distinction is a standing estate rule and the secret plane got it wrong twice:
reported as absent, an unreadable record tells a client to seed a fresh one, and the client's next
write destroys work that was really there (`service_kit.governed.user_state` documents the live case
where one field changing type made an intact row read as `exists: false`).

It is a `ServiceUnavailableError` so the shared handlers render it as a 503 problem+json — the same
fail-closed answer the user-state store gives, because the caller cannot proceed either way and the
alternative (200 with an empty inbox) is the one answer that is actively harmful.
"""

from service_kit.exceptions import ServiceUnavailableError


class InboxUnreadable(ServiceUnavailableError):
    """A stored inbox record exists but cannot be served — never reported as absent."""

    def __init__(self, partition: str, reason: str) -> None:
        super().__init__(f"the inbox {partition} record cannot be read: {reason}")
        self.partition = partition
        self.reason = reason
