"""Lineage's provenance store over an empty feed, for a test whose claim is what the relay or a door asks the store to
record: each event and each settled refusal is kept, where the real store would write them through the catalog's doors.

The feed holds no row, no run and no author, so a relayed event is never the replay of one recorded and founds its own
run. A test hands it to the code under test through `typing.cast`, as the class is no protocol: each method carries the
real method's signature, so a caller that asks with an argument the store does not take fails here as it would there.
"""

from collections.abc import Collection
from typing import Any

from lineage.models import DatasetEvent, RunEvent


class ProvenanceStoreDouble:
    """The `lineage.services.provenance.ProvenanceStore` a test reads back: :attr:`recorded`, each event in the order it
    was recorded, and :attr:`refusals`, each refusal as the keywords it was given."""

    def __init__(self) -> None:
        self.recorded: list[RunEvent | DatasetEvent] = []
        self.refusals: list[dict[str, str | None]] = []

    async def record(self, event: RunEvent | DatasetEvent, *, aliased: Collection[str]) -> None:
        """Keep ``event``."""
        del aliased
        self.recorded.append(event)

    async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
        """Keep a settled refusal."""
        self.refusals.append({"outbox_key": outbox_key, "run_id": run_id, "author": author, "reason": reason, "event_json": event_json})

    async def events_recorded(self, keys: Collection[str]) -> set[str]:
        """None of ``keys``: the feed holds no row."""
        del keys
        return set()

    async def recorded_event(self, key: str) -> dict[str, Any] | None:
        """``None``: the feed holds no row under ``key``."""
        del key
        return None

    async def run_author(self, run_id: str) -> str | None:
        """``None``: the feed binds no run to an author."""
        del run_id
        return None
