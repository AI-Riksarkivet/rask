"""Whether lineage's feed answers through the catalog, kept current in the background (LIN-8).

Readiness rests on the feed answering through the catalog, lineage's sole hard dependency. The read that shows it, the
feed's current version, costs what the catalog takes to open the feed (a describe that loads the table server-side),
and the kubelet's probe allows one second: measured live 2026-10-19, the read took 1.03-1.25 s, the probe timed out,
and the only pod left the Service, so the feed door refused every connection while nothing had failed. So the read runs
here, on its own interval and budget, and a probe reads the verdict without waiting.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress


log = logging.getLogger(__name__)


class FeedWatch:
    """The latest answer to "does the feed answer through the catalog?", kept by a background loop.

    Ready means the latest read answered within its budget and that answer is recent. A read that fails or overruns
    its budget turns the verdict at once; one that stops being made (a loop that died, a starved event loop) turns it
    once the last answer is older than two rounds.
    """

    def __init__(
        self,
        read: Callable[[], Awaitable[object]],
        *,
        interval: float,
        budget: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read
        self._interval = interval
        self._budget = budget
        self._clock = clock
        self._answered_at: float | None = None
        self._answering = False
        self._task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        if not self._answering or self._answered_at is None:
            return False
        return self._clock() - self._answered_at <= 2 * (self._interval + self._budget)

    async def check(self) -> None:
        """Read the feed once, within the budget, and keep what it showed."""
        try:
            async with asyncio.timeout(self._budget):
                await self._read()
        except Exception:
            log.warning("readyz_catalog_unavailable", exc_info=True)
            self._answering = False
            return
        self._answering = True
        self._answered_at = self._clock()

    async def _loop(self) -> None:
        while True:
            await self.check()
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="lineage-feed-watch")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
