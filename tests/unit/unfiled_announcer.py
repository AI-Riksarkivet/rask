"""An announcer for a test that calls a catalog door straight, with no control root to file on: it files nothing and
sends each announcement as the door sends it. What the filing does is pinned through the app, on a real control root, by
`services/catalog/tests/test_a_door_that_failed_after_its_change_is_announced_by_the_finish.py`."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

from catalog.api.announcing import Announcer, Filing
from catalog.services.announcement_intents import Announcement, ControlMessage, LineageMarker, Message
from lakehouse.control_emit import ControlEmitter
from lakehouse.lineage_events import DatasetUuids


class _Unfiled:
    def __init__(self, control: ControlEmitter) -> None:
        self._control = control

    def marker(
        self,
        segments: list[str],
        *,
        author: str | None,
        operation: str,
        uuids: DatasetUuids,
        source_uri: str | None = None,
        renamed_from: list[str] | None = None,
    ) -> LineageMarker:
        return LineageMarker(
            segments=segments,
            author=author,
            operation=operation,
            uuids=uuids,
            source_uri=source_uri,
            renamed_from=renamed_from,
            event_time=datetime.now(UTC).isoformat(),
        )

    @asynccontextmanager
    async def announcing(self, *, door: str, target: str, announcements: Sequence[Announcement]) -> AsyncGenerator[Filing]:
        del door, target, announcements
        yield Filing(self._send, {})

    async def _send(self, message: Message) -> None:
        if isinstance(message, ControlMessage):
            await self._control.emit(message.event)


def unfiled(control: ControlEmitter) -> Announcer:
    """An announcer that sends each control event to ``control`` and each lineage marker nowhere, filing nothing."""
    return cast(Announcer, _Unfiled(control))
