"""Dedupe that survives a pod restart, and a notification id the two lanes cannot disagree about.

`test_ingress_dedupe.py` proves the two lanes converge. This suite proves WHY they can, by attacking
the two ways the property is usually built and lost:

* **Nothing in the ingress remembers anything.** The guard is the actor's idempotency on the
  notification's natural key, so the ingress process may be replaced between two deliveries of the
  same message and the answer does not change. Every plausible cheaper guard — a process-local set, a
  time window — is reproduced below and asserted to be BROKEN, because "the same event twice lands
  one pointer" is true of a process-local set too, right up until the pod restarts.
* **The key is `(run_id, event_type)` and carries nothing else.** Fold the event time or the feed
  sequence into it and the two lanes mint different ids for one run, which is a second pointer rather
  than a failure anyone would see. Both wrong forms are built here and shown to diverge.

The JetStream duplicate window is **2 minutes** (`nats-stream-job.yaml:58`), which is why none of this
may lean on the broker: the estate has already paid once for assuming a duplicate window covers a
restart.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx

from notifications.api.ingest import ingest_run_event
from notifications.api.metrics import Lane
from notifications.api.reconciler import LineageCursor, LineageCursorStore, LineageFeedClient, reconcile
from notifications.api.visibility import Visibility
from notifications.proxies import TypedActorProxy


LINEAGE = "http://lineage.invalid"
OPEN = Visibility(client=None, enabled=False)

RUN_EVENT: dict[str, Any] = {
    "eventType": "FAIL",
    "eventTime": "2026-08-09T12:00:00+00:00",
    "run": {"runId": "run-77", "facets": {"author": {"name": "alice", "sub": "alice"}, "lance": {"run_id": "ingest-77"}}},
    "outputs": [{"namespace": "bronze", "name": "bronze$pages"}],
}


class _Inbox:
    """One subject's inbox with the REAL actor's contract: idempotent on `notification_id`."""

    def __init__(self, store: "_ActorPlane", subject: str) -> None:
        self._store = store
        self._subject = subject

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = self._store.boxes.setdefault(self._subject, [])
        if any(row["notification_id"] == payload["notification_id"] for row in rows):
            return {"delivered": False, "unread": len(rows), "rows": len(rows)}
        rows.append(payload)
        return {"delivered": True, "unread": len(rows), "rows": len(rows)}


class _ActorPlane:
    """The DURABLE half — `lance-statestore`, which outlives the pod that writes to it."""

    def __init__(self) -> None:
        self.boxes: dict[str, list[dict[str, Any]]] = {}

    def open(self, subject: str) -> TypedActorProxy:
        return cast(TypedActorProxy, _Inbox(self, subject))


class _AppendOnlyInbox:
    """An inbox with NO idempotency — the store the wrong guards below are protecting."""

    def __init__(self, store: "_AppendOnlyPlane", subject: str) -> None:
        self._store = store
        self._subject = subject

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = self._store.boxes.setdefault(self._subject, [])
        rows.append(payload)
        return {"delivered": True, "unread": len(rows), "rows": len(rows)}


class _AppendOnlyPlane:
    def __init__(self) -> None:
        self.boxes: dict[str, list[dict[str, Any]]] = {}

    def open(self, subject: str) -> TypedActorProxy:
        return cast(TypedActorProxy, _AppendOnlyInbox(self, subject))


class _MemoryCursor:
    """The reconciler's cursor, in memory. Records every write so a stalled walk is visible."""

    def __init__(self, seq: int) -> None:
        self.seq = seq
        self.writes: list[int] = []

    async def get(self) -> LineageCursor | None:
        return LineageCursor(seq=self.seq, updated_at=datetime.now(UTC))

    async def set(self, seq: int, *, resume_from: int | None = None, pending_high: int | None = None, floor: int | None = None, stalls: int = 0) -> None:
        self.writes.append(seq)
        self.seq = seq


@pytest.fixture
def plane() -> _ActorPlane:
    return _ActorPlane()


@pytest.fixture
def feed(lineage_identity_token: Path) -> LineageFeedClient:
    """A feed client over a real HTTPX transport, intercepted by respx — never a patched method."""
    return LineageFeedClient(
        client=httpx.AsyncClient(),
        base_url=LINEAGE,
        token_file=str(lineage_identity_token),
        timeout_seconds=5.0,
        page_limit=500,
    )


def _feed_page(*records: tuple[int, dict[str, Any]]) -> None:
    respx.get(f"{LINEAGE}/events").mock(
        return_value=httpx.Response(200, json={"events": [{"seq": seq, "event": event} for seq, event in records], "next_cursor": None}),
    )


async def _bus(plane: _ActorPlane, event: dict[str, Any]) -> dict[str, str]:
    return await ingest_run_event(event, lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)


# --- the key itself: what it must contain, and what would break convergence ---------------------


@pytest.mark.asyncio
@respx.mock
async def test_a_feed_re_offer_of_an_already_delivered_run_still_advances_the_cursor(plane: _ActorPlane, feed: LineageFeedClient) -> None:
    """The livelock this layer is the only one that can see.

    The reconciler holds its cursor back whenever a row asked for a RETRY. A duplicate that reported
    itself as a failure would therefore stall the mark forever: every tick re-reads the same rows,
    re-detects the same duplicates, and never moves — a reconciler that runs cleanly and reconciles
    nothing, with a healthy-looking log. So a duplicate must be a SUCCESS, and the cursor must move.
    """
    _feed_page((12, RUN_EVENT))
    await _bus(plane, RUN_EVENT)
    store = _MemoryCursor(11)

    result = await reconcile(client=feed, store=cast(LineageCursorStore, store), visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert (result.scanned, result.retried, result.cursor) == (1, 0, 12)
    assert store.writes == [12]
