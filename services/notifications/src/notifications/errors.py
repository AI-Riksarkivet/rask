"""The one domain error this plane owns.

`InboxUnreadable` exists to keep "there is nothing here" and "there is something here I refuse to
serve" apart. That distinction is a standing estate rule: reported as absent, an unreadable record
tells a client to seed a fresh one, and the client's next write destroys work that was really there
(measured live: one field changing type made an intact stored row read as `exists: false`).

It is a `ServiceUnavailableError` so the shared handlers render it as a 503 problem+json, because the
caller cannot proceed either way and the alternative (200 with an empty inbox) is the one answer that
is actively harmful.
"""

from service_kit.exceptions import ServiceUnavailableError


class InboxUnreadable(ServiceUnavailableError):
    """A stored inbox record exists but cannot be served — never reported as absent."""

    def __init__(self, partition: str, reason: str) -> None:
        super().__init__(f"the inbox {partition} record cannot be read: {reason}")
        self.partition = partition
        self.reason = reason
