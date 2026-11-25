"""A service's control emitter, for a test whose claim is what a door announces on the control lane: each event the door
emits is kept, in order, where the real emitter would sign it and publish it on the bus.

A test hands it to the app in place of the emitter its lifespan builds (`app.dependency_overrides[get_control_emitter]`).
What signing and the bus do with an event is driven through `tests.bus_double` and the seam's own tests.
"""

from lakehouse.events.control_events import CatalogControlEvent


class ControlEmitterDouble:
    """The `lakehouse.events.control_emit.ControlEmitter` a test reads back: :attr:`events`, in the order they were emitted."""

    def __init__(self) -> None:
        self.events: list[CatalogControlEvent] = []

    async def emit(self, event: CatalogControlEvent) -> None:
        """Keep ``event``."""
        self.events.append(event)
