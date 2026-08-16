"""The second ingress: the feed poll, the cursor, and the walk between them.

respx everywhere an HTTPX call is made, never a patched client method: it intercepts at the
TRANSPORT, so what is asserted is the request that would actually reach lineage rather than whether
our own function was called with the arguments we then assert it was called with.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
import respx
from pydantic import SecretStr

from notifications.api.ingest import DAPR_RETRY, DAPR_SUCCESS
from notifications.api.metrics import Lane
from notifications.api.reconciler import (
    FEED_MAX_STALLS,
    LineageCursor,
    LineageCursorStore,
    LineageCursorUnreadable,
    LineageFeedBudgetExceeded,
    LineageFeedClient,
    reconcile,
)
from notifications.api.visibility import Visibility
from notifications.proxies import TypedActorProxy


LINEAGE = "http://lineage.invalid"
OPEN = Visibility(client=None, enabled=False)
#: A module-level singleton, because a `SecretStr(...)` in a default argument is evaluated once at
#: import anyway — writing it there only hides that.
APP_TOKEN = SecretStr("app-token")


def _event(seq: int, *, run_id: str | None = None, author: str = "alice", event_type: str = "FAIL") -> dict[str, Any]:
    return {
        "seq": seq,
        "event": {
            "eventType": event_type,
            "eventTime": "2026-08-09T12:00:00+00:00",
            "run": {"runId": run_id or f"run-{seq}", "facets": {"author": {"name": author, "sub": author}}},
            "outputs": [{"namespace": "silver", "name": "silver$pages"}],
        },
    }


def _feed_client(*, token: SecretStr | None = APP_TOKEN, page_limit: int = 500) -> LineageFeedClient:
    return LineageFeedClient(
        client=httpx.AsyncClient(),
        base_url=LINEAGE,
        identity="notifications",
        token=token,
        timeout_seconds=5.0,
        page_limit=page_limit,
    )


class _Inbox:
    def __init__(self, plane: "_Plane", subject: str) -> None:
        self._plane = plane
        self._subject = subject

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._subject in self._plane.broken:
            raise RuntimeError("the sidecar is unreachable")
        rows = self._plane.boxes.setdefault(self._subject, [])
        if any(row["notification_id"] == payload["notification_id"] for row in rows):
            return {"delivered": False, "unread": len(rows), "rows": len(rows)}
        rows.append(payload)
        return {"delivered": True, "unread": len(rows), "rows": len(rows)}


class _Plane:
    def __init__(self, broken: set[str] | None = None) -> None:
        self.boxes: dict[str, list[dict[str, Any]]] = {}
        self.broken = broken or set()

    def open(self, subject: str) -> TypedActorProxy:
        return cast(TypedActorProxy, _Inbox(self, subject))


class _MemoryCursor:
    """The cursor store's contract in memory — the walk's tests are about the walk."""

    def __init__(
        self, seq: int | None = None, *, resume_from: int | None = None, pending_high: int | None = None, floor: int | None = None, stalls: int = 0
    ) -> None:
        self.seq = seq
        self.resume_from = resume_from
        self.pending_high = pending_high
        self.floor = floor
        self.stalls = stalls
        self.writes: list[int] = []
        #: Every write in full, so a test can assert on the PARKED state an interrupted walk leaves.
        self.records: list[tuple[int, int | None, int | None]] = []

    async def get(self) -> LineageCursor | None:
        if self.seq is None:
            return None
        return LineageCursor(
            seq=self.seq, updated_at=datetime.now(UTC), resume_from=self.resume_from, pending_high=self.pending_high, floor=self.floor, stalls=self.stalls
        )

    async def set(self, seq: int, *, resume_from: int | None = None, pending_high: int | None = None, floor: int | None = None, stalls: int = 0) -> None:
        self.seq = seq
        self.resume_from = resume_from
        self.pending_high = pending_high
        self.floor = floor
        self.stalls = stalls
        self.writes.append(seq)
        self.records.append((seq, resume_from, pending_high))


def _store(
    seq: int | None = None, *, resume_from: int | None = None, pending_high: int | None = None, floor: int | None = None
) -> tuple[LineageCursorStore, _MemoryCursor]:
    memory = _MemoryCursor(seq, resume_from=resume_from, pending_high=pending_high, floor=floor)
    return cast(LineageCursorStore, memory), memory


# --- the poll client: what actually reaches lineage --------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_the_poll_asks_for_the_full_payload_because_the_summary_has_no_run_id() -> None:
    """`summary=true` drops the `event` column at the SQL layer, and the feed's row carries NO
    `run_id` column in either mode — the id lives only inside that payload. A summary row therefore
    cannot produce a notification id at all, so this lane pays for the full record."""
    route = respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [], "next_cursor": None}))

    await _feed_client().page(after=None)

    assert route.calls.last.request.url.params["summary"] == "false"
    assert route.calls.last.request.url.params["limit"] == "500"
    assert "after" not in route.calls.last.request.url.params


@pytest.mark.asyncio
@respx.mock
async def test_paging_older_passes_the_cursor_as_after() -> None:
    """`?after=<seq>` walks OLDER (`WHERE seq < %s ORDER BY seq DESC`) — there is no "give me
    everything since" call to make, so catching up is a walk DOWN toward the stored mark."""
    route = respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [], "next_cursor": None}))

    await _feed_client().page(after=42)

    assert route.calls.last.request.url.params["after"] == "42"


@pytest.mark.asyncio
@respx.mock
async def test_the_service_door_is_opened_with_both_headers() -> None:
    """lineage opens its service door on the PAIR. A request carrying only `dapr-api-token` falls
    through to OIDC by design — the sidecar stamps that token on everything it delivers — so half the
    credential would produce a 401 that reads like a credential problem instead of a config one."""
    route = respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [], "next_cursor": None}))

    await _feed_client().page(after=None)

    headers = route.calls.last.request.headers
    assert headers["dapr-api-token"] == "app-token"
    assert headers["x-lance-service-identity"] == "notifications"


@pytest.mark.asyncio
@respx.mock
async def test_without_an_app_token_neither_header_is_sent() -> None:
    route = respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [], "next_cursor": None}))

    await _feed_client(token=None).page(after=None)

    headers = route.calls.last.request.headers
    assert "dapr-api-token" not in headers
    assert "x-lance-service-identity" not in headers


@pytest.mark.asyncio
@respx.mock
async def test_a_transient_failure_is_NOT_retried_in_this_module() -> None:
    """The inverse of what this test used to assert, and the change is deliberate.

    It read: "Tenacity lives HERE and nowhere else: no sidecar policy covers a call this service makes
    itself." That was true only because the feed was read over a DIRECT httpx call. Read through Dapr
    service invocation (`IngressSettings.feed_base_url`), the estate's existing `invokeRetry` policy
    covers it — and that policy already encodes the same rule the hand-written `_is_transient` did:
    `408,429,500-599`, never a 4xx.

    So a 503 must now propagate on the FIRST attempt. One layer owns redelivery; a second multiplies it.
    """
    route = respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(503))

    with pytest.raises(httpx.HTTPStatusError):
        await _feed_client().page(after=None)

    assert route.call_count == 1, (
        "the module retried the call itself, so the sidecar policy and this code both back off — the multiplication the subscription handler has always refused"
    )


@pytest.mark.asyncio
@respx.mock
async def test_a_client_error_is_not_retried() -> None:
    """A 403 means this deployment's service identity is not on lineage's allowlist, which no amount
    of backoff fixes — retrying it is pure delay in front of the same answer."""
    route = respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(403))

    with pytest.raises(httpx.HTTPStatusError):
        await _feed_client().page(after=None)

    assert route.call_count == 1


# --- the cursor store: fail-closed, and absent is not unreadable --------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_a_cursor_that_was_never_written_reads_as_absent() -> None:
    """Dapr answers a missing key with 204 — the ordinary first-tick case, not an error."""
    respx.get(url__startswith="http://localhost:3500/v1.0/state/").mock(return_value=httpx.Response(204))
    store = LineageCursorStore(client=httpx.AsyncClient(), store_name="lance-statestore")
    assert await store.get() is None


@pytest.mark.asyncio
@respx.mock
async def test_a_stored_cursor_round_trips() -> None:
    body = {"seq": 77, "updated_at": "2026-08-09T12:00:00Z"}
    respx.get(url__startswith="http://localhost:3500/v1.0/state/").mock(return_value=httpx.Response(200, json=body))
    store = LineageCursorStore(client=httpx.AsyncClient(), store_name="lance-statestore")
    cursor = await store.get()
    assert cursor is not None
    assert cursor.seq == 77


@pytest.mark.asyncio
@respx.mock
async def test_an_unreachable_store_is_unreadable_never_absent() -> None:
    """Absent means "prime and notify nobody", so an outage read as absent would jump the mark to the
    newest row and drop every notification in between — permanent, invisible loss out of a blip."""
    respx.get(url__startswith="http://localhost:3500/v1.0/state/").mock(return_value=httpx.Response(500))
    store = LineageCursorStore(client=httpx.AsyncClient(), store_name="lance-statestore")
    with pytest.raises(LineageCursorUnreadable):
        await store.get()


@pytest.mark.asyncio
@respx.mock
async def test_a_cursor_that_no_longer_fits_its_schema_is_unreadable() -> None:
    respx.get(url__startswith="http://localhost:3500/v1.0/state/").mock(return_value=httpx.Response(200, json={"seq": "yesterday"}))
    store = LineageCursorStore(client=httpx.AsyncClient(), store_name="lance-statestore")
    with pytest.raises(LineageCursorUnreadable):
        await store.get()


@pytest.mark.asyncio
@respx.mock
async def test_writing_the_cursor_uses_the_state_bulk_shape() -> None:
    route = respx.post("http://localhost:3500/v1.0/state/lance-statestore").mock(return_value=httpx.Response(204))
    store = LineageCursorStore(client=httpx.AsyncClient(), store_name="lance-statestore")

    await store.set(101)

    body = route.calls.last.request.read()
    assert b'"notifications-lineage-cursor"' in body
    assert b'"seq":101' in body


# --- the walk ----------------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_a_first_ever_tick_primes_the_cursor_and_notifies_nobody() -> None:
    """Treating everything as new on a fresh deployment means replaying the retained feed into
    inboxes on day one — the same failure `deliverPolicy: new` prevents on the bus, arriving by the
    other door."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(9), _event(8)], "next_cursor": 8}))
    plane = _Plane()
    store, memory = _store(None)

    result = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert result.primed
    assert memory.seq == 9
    assert plane.boxes == {}


@pytest.mark.asyncio
@respx.mock
async def test_only_rows_above_the_mark_are_ingested() -> None:
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(9), _event(8), _event(7), _event(6)], "next_cursor": 6}))
    plane = _Plane()
    store, memory = _store(7)

    result = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert result.scanned == 2
    assert sorted(row["notification_id"] for row in plane.boxes["alice"]) == ["run-8@FAIL", "run-9@FAIL"]
    assert memory.seq == 9


@pytest.mark.asyncio
@respx.mock
async def test_the_walk_stops_as_soon_as_the_mark_comes_into_view() -> None:
    """One page in steady state: the walk pages OLDER only while every row on the page is still above
    the mark."""
    route = respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(9), _event(8)], "next_cursor": 8}))
    store, _memory = _store(8)

    await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=_Plane().open, max_pages=5, budget_seconds=10)

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_the_walk_pages_back_until_it_reaches_the_mark() -> None:
    respx.get(f"{LINEAGE}/events").mock(
        side_effect=[
            httpx.Response(200, json={"events": [_event(9), _event(8)], "next_cursor": 8}),
            httpx.Response(200, json={"events": [_event(7), _event(6)], "next_cursor": 6}),
            httpx.Response(200, json={"events": [_event(5), _event(4)], "next_cursor": 4}),
        ]
    )
    plane = _Plane()
    store, memory = _store(5)

    result = await reconcile(client=_feed_client(page_limit=2), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert result.scanned == 4
    assert not result.truncated
    assert memory.seq == 9


@pytest.mark.asyncio
@respx.mock
async def test_an_exhausted_feed_ends_the_walk() -> None:
    """`next_cursor: null` is the feed's floor — the mark is older than anything retained, and there
    is nothing further down to ask for."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(3), _event(2)], "next_cursor": None}))
    plane = _Plane()
    store, memory = _store(1)

    result = await reconcile(client=_feed_client(page_limit=2), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert not result.truncated
    assert result.scanned == 2
    assert memory.seq == 3


@pytest.mark.asyncio
@respx.mock
async def test_a_failed_row_holds_the_mark_where_it_was() -> None:
    """Advancing past a failure is the one thing this lane cannot undo. Re-offering the rows that DID
    land is free — the actor is idempotent on the natural key."""
    page = {"events": [_event(9, author="bob"), _event(8), _event(7)], "next_cursor": 6}
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json=page))
    plane = _Plane(broken={"bob"})
    store, memory = _store(7)

    result = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert result.retried == 1
    assert result.cursor == 7
    # The MARK is what must not move. `writes` is no longer empty and that is deliberate: a retried
    # pass now persists its stall count, so a permanently failing row can be stepped over after
    # FEED_MAX_STALLS instead of blocking every newer notification forever. The write records the
    # counter; it does not advance the mark, which is what this test is about.
    assert memory.seq == 7
    assert memory.writes == [7], "a retried pass wrote a mark other than the one it was holding"
    assert memory.stalls == 1
    assert len(plane.boxes["alice"]) == 1


@pytest.mark.asyncio
@respx.mock
async def test_a_walk_that_runs_out_of_pages_says_so_and_moves_on() -> None:
    """The page budget covers the feed's whole retention by default, so exhausting it means the rows
    between here and the mark are already pruned: unrecoverable rather than merely unread. Stalling
    would buy nothing and hide it, so the mark advances and the gap is an ERROR in the log."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(90), _event(89)], "next_cursor": 89}))
    plane = _Plane()
    store, memory = _store(1)

    result = await reconcile(client=_feed_client(page_limit=2), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=1, budget_seconds=10)

    assert result.truncated
    assert memory.seq == 90


@pytest.mark.asyncio
@respx.mock
async def test_the_feed_lane_stamps_the_sequence_it_arrived_at() -> None:
    """The one lane that has a sequence number, so a stored row can say which door first told this
    subject about this run."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(9)], "next_cursor": None}))
    plane = _Plane()
    store, _memory = _store(8)

    await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert plane.boxes["alice"][0]["event_seq"] == 9


@pytest.mark.asyncio
@respx.mock
async def test_an_unreadable_cursor_stops_the_tick_rather_than_re_priming() -> None:
    respx.get(url__startswith="http://localhost:3500/v1.0/state/").mock(return_value=httpx.Response(500))
    store = LineageCursorStore(client=httpx.AsyncClient(), store_name="lance-statestore")

    with pytest.raises(LineageCursorUnreadable):
        await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=_Plane().open, max_pages=5, budget_seconds=10)


def test_the_feed_lane_is_labelled_apart_from_the_bus() -> None:
    """Two lanes on one counter, because the question an operator asks is which door stopped
    delivering — and a single `ingress.events` series cannot answer it."""
    assert {Lane.BUS.value, Lane.FEED.value} == {"bus", "feed"}


# --- the budget must BOUND the walk, not discard it ---------------------------------------------
#
# Found reviewing the S2 tick. The mark is a LOW-water one and the walk runs DOWNWARD, so a pass that
# handled the newest N rows and was then cut off cannot raise it — the rows between the mark and where
# it stopped are still unhandled. With nowhere to park the descent, every tick restarted at the newest
# row, spent the same budget re-walking the same prefix, died at the same depth and wrote nothing. A
# backlog deeper than one budget was therefore never drained: a PERMANENT stall wearing the costume of
# a transient one, and precisely the state an outage produces — which is the case §2 says this lane
# exists to cover.


def _slow_ingest(seconds: float, status: dict[str, str] | None = None) -> Callable[..., Awaitable[dict[str, str]]]:
    """An ingest that costs wall-clock, so a walk can be made to outrun its budget deterministically.

    The status is the real `DAPR_*` dict, never a bare string: `reconcile()` compares against the
    constant, so a string fake would silently never match and the RETRY test would pass while proving
    nothing. It did exactly that on the first draft of these tests.
    """

    async def _ingest(*args: object, **kwargs: object) -> dict[str, str]:
        await asyncio.sleep(seconds)
        return status if status is not None else DAPR_SUCCESS

    return _ingest


def _ingest_fast_then_stalling(fast_calls: int, stall_seconds: float, status: dict[str, str] | None = None) -> Callable[..., Awaitable[dict[str, str]]]:
    """An ingest that is FREE for `fast_calls`, then stalls — so an over-budget walk is deterministic.

    The budget test used to be a wall-clock race: every ingest cost 0.03s and the budget was 0.1s, so
    the pass had to fit a whole 2-row page (0.06s) into 0.1s to park anything at all. That is 40ms of
    slack for the HTTP mock, the parking write and event-loop scheduling — fine on an idle machine,
    and it failed in the full suite where the CPU is contended. The failure looked like the product
    bug the test exists to catch ("an over-budget walk recorded nothing"), which is the worst way for
    a flake to present.

    Making the numbers bigger would only move the race. Instead the two things the test needs are made
    INDEPENDENT of load: the first page's ingests cost nothing, so a page always completes and is
    always parked; the next one stalls for far longer than any budget, so the timeout always fires.
    Neither outcome depends on how fast the machine is.
    """
    calls = 0

    async def _ingest(*args: object, **kwargs: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls > fast_calls:
            await asyncio.sleep(stall_seconds)
        return status if status is not None else DAPR_SUCCESS

    return _ingest


def _descending_pages(top: int, per_page: int, pages: int) -> list[httpx.Response]:
    """`pages` responses walking down from `top`, each carrying `per_page` rows."""
    responses = []
    seq = top
    for _ in range(pages):
        events = [_event(seq - offset) for offset in range(per_page)]
        seq -= per_page
        responses.append(httpx.Response(200, json={"events": events, "next_cursor": seq}))
    return responses


@pytest.mark.asyncio
@respx.mock
async def test_a_walk_cut_off_by_the_budget_parks_where_it_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression: an over-budget pass used to record NOTHING and restart from the top forever."""
    # Exactly one 2-row page runs for free, so a page is ALWAYS completed and parked; the next ingest
    # stalls for 30s against a 0.5s budget, so the timeout ALWAYS fires. Neither half is a race, which
    # is what this test needs and what its wall-clock version did not have.
    monkeypatch.setattr("notifications.api.reconciler.ingest_run_event", _ingest_fast_then_stalling(fast_calls=2, stall_seconds=30))
    respx.get(f"{LINEAGE}/events").mock(side_effect=_descending_pages(top=1000, per_page=2, pages=20))
    plane = _Plane()
    store, memory = _store(1)

    with pytest.raises(LineageFeedBudgetExceeded):
        await reconcile(client=_feed_client(page_limit=2), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=20, budget_seconds=0.5)

    assert memory.records, "an over-budget walk recorded nothing — the backlog could never be drained"
    seq, resume_from, pending_high = memory.records[-1]
    assert seq == 1, "the low-water mark must NOT advance: the rows below where it stopped are unhandled"
    assert resume_from is not None and resume_from < 1000, "nothing was parked, so the next tick restarts at the top"
    assert pending_high == 1000, "the ceiling belongs to the whole multi-tick walk, not to this tick"


@pytest.mark.asyncio
@respx.mock
async def test_the_next_tick_resumes_the_descent_instead_of_restarting() -> None:
    """A parked cursor makes the following tick continue DOWNWARD from where the last one stopped."""
    route = respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(400), _event(399)], "next_cursor": None}))
    plane = _Plane()
    store, _ = _store(1, resume_from=500, pending_high=1000)

    await reconcile(client=_feed_client(page_limit=2), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert route.calls[0].request.url.params.get("after") == "500", "the walk restarted at the top — the park was ignored"


@pytest.mark.asyncio
@respx.mock
async def test_a_completed_walk_settles_the_parked_ceiling_and_clears_it() -> None:
    """Finishing a resumed walk adopts the WHOLE walk's ceiling and leaves nothing parked behind."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(4), _event(3)], "next_cursor": None}))
    plane = _Plane()
    store, memory = _store(1, resume_from=5, pending_high=9_999)

    await reconcile(client=_feed_client(page_limit=2), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    seq, resume_from, pending_high = memory.records[-1]
    assert seq == 9_999, "the settled mark must be the multi-tick ceiling, not this tick's newest row"
    assert resume_from is None and pending_high is None, "a completed walk left parked state behind"


@pytest.mark.asyncio
@respx.mock
async def test_a_retried_row_parks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parking past a row that asked for RETRY would step over the failure the mark is held for."""
    monkeypatch.setattr("notifications.api.reconciler.ingest_run_event", _slow_ingest(0.03, status=DAPR_RETRY))
    respx.get(f"{LINEAGE}/events").mock(side_effect=_descending_pages(top=1000, per_page=2, pages=20))
    plane = _Plane()
    store, memory = _store(1)

    with pytest.raises(LineageFeedBudgetExceeded):
        await reconcile(client=_feed_client(page_limit=2), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=20, budget_seconds=0.1)

    assert memory.records == [], "a RETRY row was parked past — the next tick would skip the failure"


# --- the feed lane must carry the SAME targeting the bus lane does ------------------------------
#
# The reconciler exists because a whole class of producer never reaches the bus: ingest, Ray TRAIN and
# every external OpenLineage producer emit over HTTP only. So anything the feed lane drops is dropped
# for exactly those runs — silently, because the pass still reports success.
#
# `reconcile()` took `watchers` and `push`, the cron passed both, and the call to `ingest_run_event`
# forwarded neither. Found by driving watch targeting end to end against a live cluster (the badge of
# a watcher who was not the author never moved); no unit test covered it because every existing test
# calls `reconcile()` without either argument, which is indistinguishable from the bug.


def _watched_event(seq: int, *, project: str, author: str = "bob") -> dict[str, Any]:
    """A run carrying its TENANT — the `lance` facet `project_id()` reads to find watchers."""
    event = _event(seq, author=author)
    event["event"]["run"]["facets"]["lance"] = {"project": project}
    return event


@pytest.mark.asyncio
@respx.mock
async def test_the_feed_lane_tells_a_project_watcher_not_only_the_author() -> None:
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_watched_event(9, project="p1")], "next_cursor": 8}))
    plane = _Plane()
    store, _ = _store(8)

    async def watchers_of(project: str) -> list[str]:
        return ["carol"] if project == "p1" else []

    await reconcile(
        client=_feed_client(),
        store=store,
        visibility=OPEN,
        open_inbox=plane.open,
        watchers=watchers_of,
        max_pages=5,
        budget_seconds=10,
    )

    assert "bob" in plane.boxes, "the author must still be told"
    assert "carol" in plane.boxes, "the project's watcher was never told — the feed lane dropped `watchers`"


@pytest.mark.asyncio
@respx.mock
async def test_the_feed_lane_pushes_channels_for_a_row_it_actually_wrote() -> None:
    """Email/Slack for HTTP-emitted runs. `fan_out` only pushes for a row it really wrote, so a
    re-walk cannot produce a second email — the catch-up lane needs no exemption of its own."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(9)], "next_cursor": 8}))
    plane = _Plane()
    store, _ = _store(8)
    pushed: list[str] = []

    async def push(subject: str, payload: dict[str, Any]) -> None:
        pushed.append(subject)

    await reconcile(
        client=_feed_client(),
        store=store,
        visibility=OPEN,
        open_inbox=plane.open,
        push=push,
        max_pages=5,
        budget_seconds=10,
    )

    assert pushed == ["alice"], "no channel push on the feed lane — every HTTP-emitted run is silent on email/Slack"


# --- the mark must not step over a row that had not COMMITTED yet -------------------------------
#
# `seq` is a `bigserial` (lineage repository.py:135) allocated at INSERT, and lineage's pool runs
# autocommit (core/age.py:33), so events commit independently and NOT necessarily in seq order. Two
# producers interleave: writer A takes 1000 and is still inserting when writer B takes 1001 and
# commits. A tick reading at that instant sees 1001 and not 1000, sets the mark to 1001, and every
# later tick filters it out — 1000 sits below the mark forever, its author and watchers never told,
# with no gap log, because the page was not truncated.
#
# The mark therefore trails by `FEED_OVERLAP`, CLAMPED to the cursor's `floor`. Both halves are
# load-bearing and the second is the one that is easy to miss: an unclamped overlap reaches below the
# mark a first-ever prime just set and delivers the backlog priming exists to skip.


@pytest.mark.asyncio
@respx.mock
async def test_a_row_that_committed_late_below_the_mark_is_still_delivered() -> None:
    # Mark at 1001 with a floor well below it; row 1000 only became visible afterwards.
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(1001), _event(1000)], "next_cursor": 999}))
    plane = _Plane()
    store, _ = _store(1001, floor=500)

    await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    delivered = sorted(row["notification_id"] for row in plane.boxes.get("alice", []))
    assert "run-1000@FAIL" in delivered, "the late-committing row was stepped over and is lost forever"


@pytest.mark.asyncio
@respx.mock
async def test_the_overlap_never_reaches_below_the_floor_a_prime_set() -> None:
    """The regression the first attempt at this fix shipped, caught by its own tests.

    A fresh deployment primes to the newest row and notifies nobody. If the overlap then reaches under
    that mark, the next tick delivers the retained backlog to everyone — the exact failure priming
    exists to prevent, arriving one tick later.
    """
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(1002), _event(1001), _event(1000)], "next_cursor": 999}))
    plane = _Plane()
    store, _ = _store(1001, floor=1001)  # primed here: everything at or below 1001 is skipped backlog

    await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    delivered = sorted(row["notification_id"] for row in plane.boxes.get("alice", []))
    assert delivered == ["run-1002@FAIL"], f"the overlap reached into primed-away backlog: {delivered}"


@pytest.mark.asyncio
@respx.mock
async def test_a_first_ever_prime_records_the_floor_it_skipped_to() -> None:
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(9), _event(8)], "next_cursor": 8}))
    store, memory = _store(None)

    await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=_Plane().open, max_pages=5, budget_seconds=10)

    assert memory.seq == 9
    assert memory.floor == 9, "priming skipped the backlog without recording where, so the overlap can reach it"


@pytest.mark.asyncio
@respx.mock
async def test_a_cursor_from_before_the_floor_existed_adopts_its_own_mark() -> None:
    """Migration: `floor=None` must not be read as "no floor" — that is the unclamped bug."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(1002), _event(1000)], "next_cursor": 999}))
    plane = _Plane()
    store, memory = _store(1001)  # no floor: an S1-era record

    await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    delivered = sorted(row["notification_id"] for row in plane.boxes.get("alice", []))
    assert delivered == ["run-1002@FAIL"], f"an old cursor let the overlap reach below its mark: {delivered}"
    assert memory.floor == 1001, "the adopted floor was not persisted, so the next pass is unprotected again"


# --- a poison row must not block every newer notification ----------------------------------------
#
# The mark advances only on a fully clean pass (`if not retried`), which is right for a TRANSIENT
# failure: the row is re-offered next tick and nothing is lost. It is wrong for a PERMANENT one. A
# recipient whose actor refuses forever makes every pass end `retried > 0`, so the mark never moves and
# every notification above it — for everyone else — is never delivered. The lane stalls silently: each
# tick logs `lineage_feed_reconciled` and reports progress it did not make.
#
# There is no attempt counter and no park-and-skip today, so the stall has no exit at all.


@pytest.mark.asyncio
@respx.mock
async def test_a_permanently_failing_recipient_does_not_block_every_newer_notification() -> None:
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(9)], "next_cursor": 8}))
    plane = _Plane(broken={"alice"})  # this subject's inbox refuses, every time, forever
    store, memory = _store(8, floor=0)

    for _ in range(FEED_MAX_STALLS + 1):
        await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert memory.seq == 9, "the mark never moved past a permanently failing row, so every newer notification is blocked for every other subject too"


@pytest.mark.asyncio
@respx.mock
async def test_a_transient_failure_still_holds_the_mark() -> None:
    """The behaviour the counter must NOT break: one bad tick re-offers, it does not step over."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(9)], "next_cursor": 8}))
    plane = _Plane(broken={"alice"})
    store, memory = _store(8, floor=0)

    await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert memory.seq == 8, "a single failed pass stepped over the row instead of re-offering it"


def test_a_cursor_written_by_a_newer_build_is_still_readable() -> None:
    """THE SECOND INSTANCE of the outage that took the inbox down, in a worse place.

    Observed live, in the same rollback: `ValidationError: 1 validation error for LineageCursor` ->
    `LineageCursorUnreadable` -> "the stored lineage feed cursor no longer fits its schema". Fields
    added to this record (`stalls`, `floor`) meet an older build's `extra="forbid"` and the whole
    cursor is refused — which stops the reconciler, the ingress that exists precisely because the bus
    alone is provably incomplete.

    Unreadable is DELIBERATELY fatal here, and rightly: reading a corrupt cursor as absent would jump
    the mark to the newest row and drop every notification in between — an outage becoming silent,
    permanent data loss. But version skew is not corruption. A rollback and a mixed-version rollout
    are routine, and treating a field a newer build added as a corrupt cursor turns a routine event
    into an outage of the whole lane.

    `extra="ignore"` rather than the inbox row's tolerated-value treatment, because the hazards
    differ: this record is service-internal and single-writer, so an unknown field carries no other
    subject's data — there is nothing here for `extra="forbid"` to contain.
    """
    from notifications.api.reconciler import LineageCursor

    cursor = LineageCursor.model_validate(
        {
            "seq": 4200,
            "updated_at": "2026-08-16T12:00:00Z",
            "a_field_a_later_build_added": 7,
        }
    )
    assert cursor.seq == 4200, "the high-water mark must survive a field this build cannot name"


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# The feed PRUNED rows this lane had not read yet
# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
# Lineage prunes its durable feed inline on EVERY ingest, keeping the newest N rows by seq. That prune
# has no idea this lane exists, and it cannot be given one: the cursor lives in notifications' Dapr
# state store, which lineage is not scoped to and must never be.
#
# So the loss is detected on THIS side, and until now it was not detected at all. The `truncated` flag
# catches only one shape — the walk running out of PAGES. The other shape exits through the success
# door: when the feed floor comes into view (`next_cursor is None`) the walk breaks with
# `truncated = False` and the pass reports a clean reconcile, even when the rows between the mark and
# the oldest surviving row were deleted before anyone read them. That is the exact population the bus
# cannot serve — ingest, Ray TRAIN and every external OpenLineage producer emit over HTTP only — so
# the silent case is the one that matters most.


@pytest.mark.asyncio
@respx.mock
async def test_a_feed_pruned_BELOW_the_cursor_reports_a_gap() -> None:
    """The silent shape. The feed answers one page and says it is exhausted, so the walk breaks
    happily — but its oldest surviving row is far above the mark, which means everything in between
    was pruned before this lane read it. Unrecoverable, and previously reported by nothing."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(9000)], "next_cursor": None, "oldest_seq": 8999}))
    plane = _Plane()
    store, _memory = _store(5)

    result = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=40, budget_seconds=10)

    assert result.gapped is True, "rows 6..8998 were pruned unread and the pass called itself clean"
    assert not result.truncated, "this is the pruned-below-the-mark shape, not the out-of-pages one"


@pytest.mark.asyncio
@respx.mock
async def test_a_feed_that_still_HOLDS_the_cursor_reports_no_gap() -> None:
    """The ordinary case, and the one a false positive would ruin: the mark is inside the retained
    window, so nothing was lost and the pass must not cry wolf every tick."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(9000)], "next_cursor": None, "oldest_seq": 1}))
    plane = _Plane()
    store, _memory = _store(8990)

    result = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=40, budget_seconds=10)

    assert result.gapped is False


@pytest.mark.asyncio
@respx.mock
async def test_a_feed_that_does_not_REPORT_its_floor_is_not_a_gap() -> None:
    """`oldest_seq` is additive, so a lineage older than this change omits it. Absent must mean "I
    cannot tell", never "a gap" — a detector that fires on every tick against a healthy older
    deployment is one nobody will keep listening to."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(9000)], "next_cursor": None}))
    plane = _Plane()
    store, _memory = _store(5)

    result = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=40, budget_seconds=10)

    assert result.gapped is False


@pytest.mark.asyncio
@respx.mock
async def test_a_gap_still_lets_the_pass_deliver_and_advance() -> None:
    """A gap is a REPORT, not a stall. The rows below the floor are gone whatever this pass does, so
    holding the mark would forfeit every row above them too — the same reasoning the out-of-pages
    branch already applies."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_event(9000)], "next_cursor": None, "oldest_seq": 8999}))
    plane = _Plane()
    store, memory = _store(5)

    result = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=40, budget_seconds=10)

    assert result.gapped is True
    assert result.scanned == 1, "the row that DID survive must still be delivered"
    assert memory.seq == 9000, "the mark must advance past a loss it cannot undo"
