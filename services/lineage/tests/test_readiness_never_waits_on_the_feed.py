"""Lineage's readiness answers without waiting for the feed read, and refuses once the feed stops answering (LIN-8).

The probe the chart sets allows one second, and the read readiness rests on, the feed's current version through the
catalog, costs what the catalog takes to open the feed. Measured live 2026-10-19 at helm rev 4: 1.03-1.25 s, so the
only pod left the Service and the feed door refused every connection. Driven through lineage's own app and lifespan,
with the provenance store replaced by one whose feed read does not return until the test lets it, then fails.
"""

import asyncio
import threading
import time
from functools import partial

import pytest
from starlette.testclient import TestClient

from lakehouse.process.health import HealthWatch
from lakehouse.process.own_threads import ThreadSet
from lineage import main
from lineage.services.catalog_reader import CatalogAnsweredError


#: How long a wait below lasts before the test gives up: a guard against a hang, never what is asserted.
_GIVE_UP_SECONDS = 30.0


class _Feed:
    """A provenance store whose feed read is held until ``released``, then answers, or fails while ``failing``."""

    def __init__(self) -> None:
        self.released = threading.Event()
        self.failing = False
        self.started = 0
        self.returned = 0

    async def ensure(self) -> None:
        return None

    async def ready(self, threads: ThreadSet) -> None:
        del threads
        self.started += 1
        while not self.released.is_set():
            await asyncio.sleep(0.01)
        self.returned += 1
        if self.failing:
            raise CatalogAnsweredError("the catalog does not answer")


def _until(client: TestClient, status: int, *, within: float) -> int:
    deadline = time.monotonic() + within
    while (answer := client.get("/readyz").status_code) != status and time.monotonic() < deadline:
        time.sleep(0.05)
    return answer


def test_readiness_answers_at_once_and_refuses_once_the_feed_stops_answering(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _Feed()
    monkeypatch.setattr(main.ProvenanceStore, "from_settings", lambda _settings, _clients: feed)
    monkeypatch.setattr(main, "HealthWatch", partial(HealthWatch, interval=0.05))
    with TestClient(main.app) as client:
        deadline = time.monotonic() + _GIVE_UP_SECONDS
        while not feed.started and time.monotonic() < deadline:
            time.sleep(0.01)
        # The read is let go at the give-up time whatever happens, so a probe that waited on it answers then, with the
        # read returned, and fails the assertion below where it would otherwise hang the run.
        give_up = threading.Timer(_GIVE_UP_SECONDS, feed.released.set)
        give_up.start()
        try:
            answer = client.get("/readyz").status_code
            answered_with = (feed.started > 0, feed.returned, answer)
        finally:
            give_up.cancel()
        # The probe was answered while the read its verdict rests on was still held: it waited on no read.
        assert answered_with == (True, 0, 503)

        feed.released.set()
        assert _until(client, 200, within=_GIVE_UP_SECONDS) == 200, "the feed answered and lineage never became ready"
        feed.failing = True
        assert _until(client, 503, within=_GIVE_UP_SECONDS) == 503, "the feed stopped answering and lineage still reports ready"
