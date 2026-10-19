"""Lineage's readiness answers at once whatever the feed read costs, and refuses once the feed stops answering (LIN-8).

The probe the chart sets allows one second, and the read readiness rests on, the feed's current version through the
catalog, costs what the catalog takes to open the feed. Measured live 2026-10-19 at helm rev 4: 1.03-1.25 s, so the
only pod left the Service and the feed door refused every connection. Driven through lineage's own app and lifespan,
with the provenance store replaced by one whose feed read takes longer than the probe allows, then fails.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from starlette.testclient import TestClient

from lineage import main
from service_kit.exceptions import ServiceUnavailableError


#: Longer than the one second the chart's probe allows.
READ_SECONDS = 1.5


class _Feed:
    """A provenance store whose feed read takes ``delay`` seconds, then answers, or fails while ``failing``."""

    def __init__(self) -> None:
        self.delay = READ_SECONDS
        self.failing = False

    async def ensure(self) -> None:
        return None

    async def ready(self) -> None:
        await asyncio.sleep(self.delay)
        if self.failing:
            raise ServiceUnavailableError("the catalog does not answer")


def _until(client: TestClient, status: int, *, within: float) -> int:
    deadline = time.monotonic() + within
    while (answer := client.get("/readyz").status_code) != status and time.monotonic() < deadline:
        time.sleep(0.05)
    return answer


def test_readiness_answers_at_once_and_refuses_once_the_feed_stops_answering(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _Feed()
    monkeypatch.setattr(main.ProvenanceStore, "from_settings", lambda _settings: feed)
    monkeypatch.setattr(main, "FEED_WATCH_INTERVAL_SECONDS", 0.05, raising=False)
    with TestClient(main.app) as client:
        asked = time.monotonic()
        client.get("/readyz")
        answered_in = time.monotonic() - asked
        assert answered_in < 0.5, f"/readyz waited {answered_in:.2f} s on a feed read the probe allows one second for"

        assert _until(client, 200, within=READ_SECONDS * 3) == 200, "the feed answered, slowly, and lineage never became ready"
        feed.delay, feed.failing = 0.0, True
        assert _until(client, 503, within=2.0) == 503, "the feed stopped answering and lineage still reports ready"
