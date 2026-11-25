"""What a service counts of the events one of its lanes did not get onto the bus.

Every lane of every service counts on ``<service>.<lane>_emit.failed``, by why (``lance.<service>.reason``) and by whether
the lane's outbox holds the event for its relay to deliver (``lance.<service>.staged``). A publish the bus refused or did
not answer is staged where the outbox took the event first; an event withheld before its publish (no signature could be
made for it, or no run id derived) is never staged. One shape for every lane is what lets an alert tell an event that
waits in an outbox from one no outbox holds, which is lost, on any lane.
"""

from typing import Literal

from opentelemetry import metrics


#: Why an event was withheld before its publish: no signature could be made for it (`unsigned`), or the catalog could
#: derive no run id for its announcement (`derive`).
type Withheld = Literal["unsigned", "derive"]


class Undelivered:
    """``<service>.<lane>_emit.failed``: one service's count of one lane's events that did not reach the bus.

    Built with the emitter that counts on it, so a meter provider a test installs before building the emitter reads it.
    """

    def __init__(self, service: str, lane: str, *, description: str, withheld: tuple[Withheld, ...] = ("unsigned",)) -> None:
        self._service = service
        self._counter = metrics.get_meter(f"lance.{service}").create_counter(f"{service}.{lane}_emit.failed", unit="{event}", description=description)
        # Every series at 0 from the start: one born at its first failure has no 0 before it, so an alert reading the
        # counter's increase would miss that failure.
        for reason, staged in (("publish", True), ("publish", False), *((reason, False) for reason in withheld)):
            self._counter.add(0, self._attributes(reason, staged=staged))

    def unpublished(self, *, staged: bool) -> None:
        """Count an event whose publish the bus refused or did not answer, and whether the outbox holds it."""
        self._counter.add(1, self._attributes("publish", staged=staged))

    def withheld(self, reason: Withheld) -> None:
        """Count an event withheld before its publish, which no outbox holds."""
        self._counter.add(1, self._attributes(reason, staged=False))

    def _attributes(self, reason: str, *, staged: bool) -> dict[str, str]:
        return {f"lance.{self._service}.reason": reason, f"lance.{self._service}.staged": "true" if staged else "false"}


__all__ = ["Undelivered", "Withheld"]
