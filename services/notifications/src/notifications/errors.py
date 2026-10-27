"""The two domain errors this plane owns.

`InboxUnreadable` exists to keep "there is nothing here" and "there is something here I refuse to
serve" apart. That distinction is a standing estate rule: reported as absent, an unreadable record
tells a client to seed a fresh one, and the client's next write destroys work that was really there
(measured live: one field changing type made an intact stored row read as `exists: false`).

It is a `ServiceUnavailableError` so the shared handlers render it as a 503 problem+json, because the
caller cannot proceed either way and the alternative (200 with an empty inbox) is the one answer that
is actively harmful.

`ActorPlaneUnavailable` is the other 503, and the two must not be confused: it says the sidecar or the actor plane
behind it could not answer, which passes, so it carries `Retry-After`; `InboxUnreadable` says a stored record will not
be served, which waiting does not change, so it carries none.
"""

from service_kit.exceptions import ServiceUnavailableError


class InboxUnreadable(ServiceUnavailableError):
    """A stored inbox record exists but cannot be served — never reported as absent."""

    def __init__(self, partition: str, reason: str) -> None:
        super().__init__(f"the inbox {partition} record cannot be read: {reason}")
        self.partition = partition
        self.reason = reason


#: How long a caller waits before trying an actor call again: a sidecar restart or a placement re-balance takes seconds.
RETRY_AFTER_SECONDS = 5


class ActorPlaneUnavailable(ServiceUnavailableError):
    """The sidecar could not be reached, or it answered that the actor plane could not serve the call (CTL-023)."""

    def __init__(self) -> None:
        super().__init__("the notifications actor plane is unavailable; try again shortly", headers={"Retry-After": str(RETRY_AFTER_SECONDS)})
