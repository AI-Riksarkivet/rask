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

    def __init__(self, seq: int | None = None, *, resume_from: int | None = None, pending_high: int | None = None) -> None:
        self.seq = seq
        self.resume_from = resume_from
        self.pending_high = pending_high
        self.writes: list[int] = []
        #: Every write in full, so a test can assert on the PARKED state an interrupted walk leaves.
        self.records: list[tuple[int, int | None, int | None]] = []

    async def get(self) -> LineageCursor | None:
        if self.seq is None:
            return None
        return LineageCursor(seq=self.seq, updated_at=datetime.now(UTC), resume_from=self.resume_from, pending_high=self.pending_high)

    async def set(self, seq: int, *, resume_from: int | None = None, pending_high: int | None = None) -> None:
        self.seq = seq
        self.resume_from = resume_from
        self.pending_high = pending_high
        self.writes.append(seq)
        self.records.append((seq, resume_from, pending_high))


def _store(seq: int | None = None, *, resume_from: int | None = None, pending_high: int | None = None) -> tuple[LineageCursorStore, _MemoryCursor]:
    memory = _MemoryCursor(seq, resume_from=resume_from, pending_high=pending_high)
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
async def test_a_transient_failure_is_retried_on_this_services_own_egress() -> None:
    """Tenacity lives HERE and nowhere else: no sidecar policy covers a call this service makes
    itself. The subscription handler has the opposite rule."""
    route = respx.get(f"{LINEAGE}/events").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"events": [_event(9)], "next_cursor": None}),
        ]
    )

    page = await _feed_client().page(after=None)

    assert [record.seq for record in page.events] == [9]
    assert route.call_count == 2


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
    assert memory.writes == []
    assert memory.seq == 7
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
    monkeypatch.setattr("notifications.api.reconciler.ingest_run_event", _slow_ingest(0.03))
    respx.get(f"{LINEAGE}/events").mock(side_effect=_descending_pages(top=1000, per_page=2, pages=20))
    plane = _Plane()
    store, memory = _store(1)

    with pytest.raises(LineageFeedBudgetExceeded):
        await reconcile(client=_feed_client(page_limit=2), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=20, budget_seconds=0.1)

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
