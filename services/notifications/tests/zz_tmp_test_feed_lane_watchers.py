"""Adversarial repro: does the FEED lane forward watchers/push the way the BUS lane does?"""

from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
import respx

from notifications.api.ingest import ingest_run_event
from notifications.api.metrics import Lane
from notifications.api.reconciler import LineageCursor, LineageCursorStore, LineageFeedClient, reconcile
from notifications.api.visibility import Visibility
from notifications.proxies import TypedActorProxy


class _Inbox:
    def __init__(self, plane, subject):
        self._plane = plane
        self._subject = subject

    async def deliver(self, payload):
        rows = self._plane.boxes.setdefault(self._subject, [])
        if any(row["notification_id"] == payload["notification_id"] for row in rows):
            return {"delivered": False}
        rows.append(payload)
        return {"delivered": True}


class _Plane:
    def __init__(self):
        self.boxes: dict[str, list[dict[str, Any]]] = {}

    def open(self, subject):
        return cast(TypedActorProxy, _Inbox(self, subject))


class _MemoryCursor:
    def __init__(self, seq):
        self.seq = seq

    async def get(self):
        return LineageCursor(seq=self.seq, updated_at=datetime.now(UTC))

    async def set(self, seq, *, resume_from=None, pending_high=None):
        self.seq = seq


def _store(seq):
    m = _MemoryCursor(seq)
    return m, m


LINEAGE = "http://lineage.invalid"
OPEN = Visibility(client=None, enabled=False)


def _event(seq: int) -> dict[str, Any]:
    return {
        "seq": seq,
        "event": {
            "eventType": "COMPLETE",
            "eventTime": "2026-08-09T12:00:00+00:00",
            "run": {
                "runId": f"run-{seq}",
                "facets": {
                    "author": {"name": "alice", "sub": "alice"},
                    "lance": {"run_id": f"src-{seq}", "project": "P"},
                },
            },
            "outputs": [{"namespace": "silver", "name": "silver$pages"}],
        },
    }


def _feed_client() -> LineageFeedClient:
    return LineageFeedClient(client=httpx.AsyncClient(), base_url=LINEAGE, identity="notifications", token=None, timeout_seconds=5.0, page_limit=500)


@pytest.mark.asyncio
@respx.mock
async def test_feed_lane_drops_watchers_and_push() -> None:
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(9)], "next_cursor": None}))
    plane = _Plane()
    store, memory = _store(8)

    asked: list[str] = []
    pushed: list[str] = []

    async def watchers_of(project_id: str) -> list[str]:
        asked.append(project_id)
        return ["bob"]

    async def push(subject: str, payload: dict[str, Any]) -> None:
        pushed.append(subject)

    result = await reconcile(
        client=_feed_client(),
        store=cast(LineageCursorStore, store),
        visibility=OPEN,
        open_inbox=plane.open,
        watchers=watchers_of,
        push=push,
        max_pages=5,
        budget_seconds=10,
    )

    print("FEED  boxes:", sorted(plane.boxes), "watcher lookups:", asked, "pushes:", pushed, "cursor:", memory.seq, result)

    # The BUS lane, same event, same helpers.
    bus_plane = _Plane()
    bus_asked: list[str] = []
    bus_pushed: list[str] = []

    async def bus_watchers(project_id: str) -> list[str]:
        bus_asked.append(project_id)
        return ["bob"]

    async def bus_push(subject: str, payload: dict[str, Any]) -> None:
        bus_pushed.append(subject)

    await ingest_run_event(
        _event(9)["event"],
        lane=Lane.BUS,
        visibility=OPEN,
        open_inbox=bus_plane.open,
        watchers=bus_watchers,
        push=bus_push,
    )
    print("BUS   boxes:", sorted(bus_plane.boxes), "watcher lookups:", bus_asked, "pushes:", bus_pushed)

    assert sorted(bus_plane.boxes) == ["alice", "bob"]
    assert sorted(plane.boxes) == ["alice", "bob"], "feed lane dropped the watcher"
