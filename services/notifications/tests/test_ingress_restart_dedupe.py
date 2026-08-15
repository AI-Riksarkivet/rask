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

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
import respx
from pydantic import SecretStr

from notifications.api.ingest import DAPR_SUCCESS, ingest_run_event
from notifications.api.lineage_events import LineageRunEvent, notifiable
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

    async def set(self, seq: int, *, resume_from: int | None = None, pending_high: int | None = None, floor: int | None = None) -> None:
        self.writes.append(seq)
        self.seq = seq


@pytest.fixture
def plane() -> _ActorPlane:
    return _ActorPlane()


@pytest.fixture
def feed() -> LineageFeedClient:
    """A feed client over a real HTTPX transport, intercepted by respx — never a patched method."""
    return LineageFeedClient(
        client=httpx.AsyncClient(),
        base_url=LINEAGE,
        identity="notifications",
        token=SecretStr("app-token"),
        timeout_seconds=5.0,
        page_limit=500,
    )


def _feed_page(*records: tuple[int, dict[str, Any]]) -> None:
    respx.get(f"{LINEAGE}/events").mock(
        return_value=httpx.Response(200, json={"events": [{"seq": seq, "event": event} for seq, event in records], "next_cursor": None}),
    )


async def _bus(plane: _ActorPlane, event: dict[str, Any]) -> dict[str, str]:
    return await ingest_run_event(event, lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)


# --- the restart: the guard is durable state, not process memory -------------------------------


@pytest.mark.asyncio
async def test_a_redelivery_after_a_pod_restart_still_lands_one_pointer(plane: _ActorPlane) -> None:
    """The restart is the case the broker cannot cover. JetStream's duplicate window is 2 minutes and
    a redelivery after a rollout is outside it by construction — an unacked message is re-offered to
    the NEW pod, which has none of the old one's memory. The only thing both processes share is the
    inbox, so the inbox has to be where the guard lives."""
    assert await _bus(plane, RUN_EVENT) is DAPR_SUCCESS
    assert len(plane.boxes["alice"]) == 1

    # The rollout: everything the ingress held is gone. The state store is not.
    restarted = _ActorPlane()
    restarted.boxes = plane.boxes

    assert await _bus(restarted, RUN_EVENT) is DAPR_SUCCESS
    assert len(restarted.boxes["alice"]) == 1


@pytest.mark.asyncio
async def test_a_process_local_dedupe_would_not_survive_the_restart() -> None:
    """The wrong guard, built and shown broken — a `set()` of handled ids in the handler.

    It passes "the same event twice lands one pointer" perfectly, which is exactly why it is the shape
    that gets written: the test that would have caught it is the restart, and only the restart.
    """
    seen: set[str] = set()

    async def ingest_with_process_local_memory(plane: _AppendOnlyPlane, event: dict[str, Any]) -> None:
        notice = notifiable(LineageRunEvent.model_validate(event))
        assert notice is not None
        if notice.delivery.notification_id in seen:
            return
        seen.add(notice.delivery.notification_id)
        await plane.open(notice.author).deliver(notice.delivery.model_dump(mode="json"))

    store = _AppendOnlyPlane()
    await ingest_with_process_local_memory(store, RUN_EVENT)
    await ingest_with_process_local_memory(store, RUN_EVENT)
    assert len(store.boxes["alice"]) == 1, "the old form does dedupe inside one process — that is what makes it convincing"

    seen.clear()  # the rollout
    await ingest_with_process_local_memory(store, RUN_EVENT)
    assert len(store.boxes["alice"]) == 2, "and this is the second notification a user would have received"


@pytest.mark.asyncio
async def test_a_two_minute_dedupe_window_would_not_cover_a_redelivery_hours_later() -> None:
    """The other wrong guard: leaning on the broker's own duplicate window.

    `nats-stream-job.yaml:58` sets it to 2 minutes. A message that fails, backs off, and is re-offered
    after the sidecar's schedule — or one re-published by an operator replaying a DLQ — is outside it,
    and the window has no idea a pod ever restarted. Reproduced with the same 2 minutes so the number
    that makes it fail is the estate's real one.
    """
    window = timedelta(minutes=2)
    handled: dict[str, datetime] = {}

    def within_window(notification_id: str, now: datetime) -> bool:
        last = handled.get(notification_id)
        handled[notification_id] = now
        return last is not None and now - last <= window

    first = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    assert not within_window("run-77@FAIL", first)
    assert within_window("run-77@FAIL", first + timedelta(seconds=30)), "inside the window it looks like a working dedupe"
    assert not within_window("run-77@FAIL", first + timedelta(hours=3)), "and outside it the same run is announced again"


@pytest.mark.asyncio
async def test_the_natural_key_holds_however_long_the_gap_is(plane: _ActorPlane) -> None:
    """The real path, driven across the same gap that breaks the window above. Nothing in it reads a
    clock, so there is no interval at which it starts letting a second pointer through."""
    later = dict(RUN_EVENT, eventTime="2026-08-09T18:45:00+00:00")

    await _bus(plane, RUN_EVENT)
    await _bus(plane, later)

    assert len(plane.boxes["alice"]) == 1


# --- the key itself: what it must contain, and what would break convergence ---------------------


@pytest.mark.asyncio
async def test_two_genuinely_different_runs_land_two_pointers(plane: _ActorPlane) -> None:
    """The other half of dedupe, and the half a tautology would skip: an id scheme that collapsed
    everything would pass every test above."""
    await _bus(plane, RUN_EVENT)
    await _bus(plane, {**RUN_EVENT, "run": {**RUN_EVENT["run"], "runId": "run-78"}})

    assert sorted(row["notification_id"] for row in plane.boxes["alice"]) == ["run-77@FAIL", "run-78@FAIL"]


def test_an_id_that_carried_the_event_time_would_mint_a_second_pointer_per_retry() -> None:
    """The wrong key, built and shown to diverge. lineage carries a partial unique index on
    `(run_id, event_type)` precisely because a RETRY-after-partial-success re-emits a run's terminal
    event with a fresh `eventTime`; a notification id that included the instant would therefore
    announce the same failure twice."""
    retried = dict(RUN_EVENT, eventTime="2026-08-09T18:45:00+00:00")

    def id_with_instant(event: dict[str, Any]) -> str:
        return f"{event['run']['runId']}@{event['eventType']}@{event['eventTime']}"

    assert id_with_instant(RUN_EVENT) != id_with_instant(retried), "the old form: two ids for one run"

    first = notifiable(LineageRunEvent.model_validate(RUN_EVENT))
    again = notifiable(LineageRunEvent.model_validate(retried))
    assert first is not None
    assert again is not None
    assert first.delivery.notification_id == again.delivery.notification_id


@pytest.mark.asyncio
@respx.mock
async def test_an_id_that_carried_the_feed_sequence_would_break_the_two_lane_convergence(plane: _ActorPlane, feed: LineageFeedClient) -> None:
    """The other wrong key, and the one that would look correct on either lane alone.

    `event_seq` exists only on the feed, so an id built from it makes the bus's id and the feed's id
    different by construction — and the two-ingress design's whole premise is that they are the same.
    The real pointer proves it: one row, and its id names the run and the state and nothing else.
    """
    _feed_page((12, RUN_EVENT))
    await _bus(plane, RUN_EVENT)
    store = _MemoryCursor(11)

    await reconcile(client=feed, store=cast(LineageCursorStore, store), visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert len(plane.boxes["alice"]) == 1
    assert plane.boxes["alice"][0]["notification_id"] == "run-77@FAIL"


@pytest.mark.asyncio
@respx.mock
async def test_the_row_that_survives_belongs_to_the_lane_that_arrived_first(plane: _ActorPlane, feed: LineageFeedClient) -> None:
    """A duplicate is not an update. The stored pointer says which door first told this subject about
    this run, so the bus's row keeps its absent sequence when the feed later re-offers the same event
    — the alternative would silently rewrite history on every reconciler tick."""
    _feed_page((12, RUN_EVENT))
    await _bus(plane, RUN_EVENT)
    assert plane.boxes["alice"][0]["event_seq"] is None

    await reconcile(
        client=feed,
        store=cast(LineageCursorStore, _MemoryCursor(11)),
        visibility=OPEN,
        open_inbox=plane.open,
        max_pages=5,
        budget_seconds=10,
    )

    assert plane.boxes["alice"][0]["event_seq"] is None


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


# --- concurrency: what this layer can and cannot hold --------------------------------------------


@pytest.mark.asyncio
async def test_two_lanes_racing_one_run_are_serialized_by_the_inbox_and_by_nothing_here() -> None:
    """The boundary condition this layer states rather than gates.

    Both ingress paths call the same function with no shared state, so nothing in the ingress
    serializes two deliveries of one run — the guard is a check-then-write INSIDE the inbox, and it is
    only atomic because an actor has a single activation and turn-based concurrency. A store without
    that lock double-writes, which is what this reproduces: an inbox that yields between its read and
    its write loses the race, and the ingress cannot help it.

    What this layer CANNOT see: that the deployed Dapr actor really is single-activation. That is a
    placement property of the runtime and only an in-cluster drive can hold it.
    """
    rows: list[str] = []

    async def deliver_without_a_lock(notification_id: str) -> None:
        already = notification_id in rows
        await asyncio.sleep(0)  # the await a real store makes between its read and its write
        if not already:
            rows.append(notification_id)

    await asyncio.gather(deliver_without_a_lock("run-77@FAIL"), deliver_without_a_lock("run-77@FAIL"))

    assert rows == ["run-77@FAIL", "run-77@FAIL"], "without the actor's turn, check-then-write is not a guard at all"
