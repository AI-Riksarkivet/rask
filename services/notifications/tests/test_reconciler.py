"""The second ingress: the feed poll, the cursor, and the walk between them.

respx everywhere an HTTPX call is made, never a patched client method: it intercepts at the
TRANSPORT, so what is asserted is the request that would actually reach lineage rather than whether
our own function was called with the arguments we then assert it was called with.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx

from notifications.api.ingest import DAPR_SUCCESS
from notifications.api.reconciler import (
    FEED_MAX_STALLS,
    LineageCursor,
    LineageCursorStore,
    LineageCursorUnreadable,
    LineageFeedBudgetExceeded,
    LineageFeedClient,
    reconcile,
)
from notifications.api.settings import IngressSettings
from notifications.api.visibility import Visibility
from notifications.proxies import TypedActorProxy
from service_kit.governed.machine_identity import IdentityTokenUnavailableError


LINEAGE = "http://lineage.invalid"
OPEN = Visibility(client=None, enabled=False)

pytestmark = pytest.mark.usefixtures("lineage_identity_token")


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


def _feed_client(*, page_limit: int = 500) -> LineageFeedClient:
    return LineageFeedClient(
        client=httpx.AsyncClient(),
        base_url=LINEAGE,
        token_file=IngressSettings().lineage_identity_token_file,
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

    def __init__(self, seq: int | None = None, *, stalls: int = 0) -> None:
        self.seq = seq
        self.stalls = stalls
        self.writes: list[int] = []

    async def get(self) -> LineageCursor | None:
        if self.seq is None:
            return None
        return LineageCursor(seq=self.seq, updated_at=datetime.now(UTC), stalls=self.stalls)

    async def set(self, seq: int, *, stalls: int = 0) -> None:
        self.seq = seq
        self.stalls = stalls
        self.writes.append(seq)


def _store(seq: int | None = None) -> tuple[LineageCursorStore, _MemoryCursor]:
    memory = _MemoryCursor(seq)
    return cast(LineageCursorStore, memory), memory


def _forward(*rows: int | dict[str, Any], oldest_seq: int | None = None) -> Callable[[httpx.Request], httpx.Response]:
    """Lineage's `/events/projection` over `rows`, answering the request's own `since` and `limit`.

    Forward pages oldest first with lineage's cursor rule (`events.py::_forward_cursor`), a `since` past the
    newest row answered with that row's seq; a request with no `since` gets the newest page, which is the
    priming read.
    """
    events = sorted((row if isinstance(row, dict) else _event(row) for row in rows), key=lambda event: event["seq"])
    head = events[-1]["seq"] if events else 0

    def respond(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params["limit"])
        since = request.url.params.get("since")
        if since is None:
            return httpx.Response(200, json={"events": events[::-1][:limit], "next_cursor": None, "oldest_seq": oldest_seq})
        page = [event for event in events if event["seq"] > int(since)][:limit]
        next_cursor = page[-1]["seq"] if page else min(int(since), head)
        return httpx.Response(200, json={"events": page, "next_cursor": next_cursor, "oldest_seq": oldest_seq})

    return respond


# --- the poll client: what actually reaches lineage --------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_each_poll_presents_the_token_the_kubelet_last_wrote_and_no_claimed_name(lineage_identity_token: Path) -> None:
    """Lineage derives the caller from the verified service-account token, so the token is the whole
    credential. The kubelet rewrites the projected file at ~515 s of a 600 s life (LH-220 probe d), so
    a client holding the boot-time value is refused ten minutes in."""
    route = respx.get(f"{LINEAGE}/events/projection").mock(return_value=httpx.Response(200, json={"events": [], "next_cursor": None}))
    client = _feed_client()

    await client.page(since=None)
    lineage_identity_token.write_text("sa-token-two\n")
    await client.page(since=None)

    sent = [call.request.headers for call in route.calls]
    assert [headers.get("authorization") for headers in sent] == ["Bearer sa-token-one", "Bearer sa-token-two"]
    assert all("x-lance-service-identity" not in headers and "dapr-api-token" not in headers for headers in sent)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.usefixtures("respx_allows_unused_routes")
async def test_an_unreadable_identity_token_fails_the_poll_and_sends_nothing(lineage_identity_token: Path) -> None:
    """An anonymous walk would be answered with an empty governed feed, an inbox quietly incomplete;
    the poll fails (503 at the cron route) instead."""
    route = respx.get(f"{LINEAGE}/events/projection").mock(return_value=httpx.Response(200, json={"events": [], "next_cursor": None}))
    lineage_identity_token.unlink()

    with pytest.raises(IdentityTokenUnavailableError):
        await _feed_client().page(since=None)

    assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_a_transient_failure_is_NOT_retried_in_this_module() -> None:
    """Redelivery is the sidecar's, not this module's.

    The feed is read through Dapr service invocation (`IngressSettings.feed_base_url`), and the
    invocation Resiliency scoped to this app's sidecar retries it on `408,429,500-599`, never a 4xx
    (tests/unit/test_invariants.py::test_the_notifications_pod_asks_for_a_sidecar_and_is_allowed_to_receive_one).

    So a 503 must now propagate on the FIRST attempt. One layer owns redelivery; a second multiplies it.
    """
    route = respx.get(f"{LINEAGE}/events/projection").mock(return_value=httpx.Response(503))

    with pytest.raises(httpx.HTTPStatusError):
        await _feed_client().page(since=None)

    assert route.call_count == 1, (
        "the module retried the call itself, so the sidecar policy and this code both back off — the multiplication the subscription handler has always refused"
    )


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
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(8, 9))
    plane = _Plane()
    store, memory = _store(None)

    result = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert result.primed
    assert memory.seq == 9
    assert plane.boxes == {}


@pytest.mark.asyncio
@respx.mock
async def test_the_walk_follows_forward_from_the_mark_until_caught_up() -> None:
    """Only rows after the mark are offered, oldest first, across pages, and the mark lands on the last."""
    route = respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(4, 5, 6, 7, 8, 9))
    plane = _Plane()
    store, memory = _store(5)

    result = await reconcile(client=_feed_client(page_limit=2), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert [row["notification_id"] for row in plane.boxes["alice"]] == ["run-6@FAIL", "run-7@FAIL", "run-8@FAIL", "run-9@FAIL"]
    assert (result.scanned, result.behind, memory.seq) == (4, False, 9)
    assert route.calls[0].request.url.params.get("since") == "5"


@pytest.mark.asyncio
@respx.mock
async def test_a_mark_taken_on_another_feed_rereads_this_one_from_its_oldest_row() -> None:
    """A mark this feed never issued (lineage's store moved, or was restored) is re-read from the feed's oldest row,
    rather than waiting for the new feed to climb past a seq it never had."""
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(1, 2, 3, oldest_seq=1))
    plane = _Plane()
    store, memory = _store(121_000)

    result = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert [row["notification_id"] for row in plane.boxes["alice"]] == ["run-1@FAIL", "run-2@FAIL", "run-3@FAIL"]
    assert (result.gapped, memory.seq) == (False, 3)


@pytest.mark.asyncio
@respx.mock
async def test_a_failed_row_stops_the_mark_just_before_it() -> None:
    """Advancing past a failure is the one thing this lane cannot undo. Rows before it are kept: the
    next tick starts at the failed row rather than re-walking what already landed."""
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(8, _event(9, author="bob"), 10))
    plane = _Plane(broken={"bob"})
    store, memory = _store(7)

    result = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert (result.retried, result.cursor, memory.seq, memory.stalls) == (1, 8, 8, 1)
    assert [row["notification_id"] for row in plane.boxes["alice"]] == ["run-8@FAIL"], "a row after the failure was delivered out of order"


@pytest.mark.asyncio
@respx.mock
async def test_a_walk_that_uses_its_page_budget_keeps_its_progress_and_says_it_is_behind() -> None:
    """Not a loss: the mark records every row handled, and the next tick continues from it."""
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(2, 3, 4, 5, 6))
    plane = _Plane()
    store, memory = _store(1)

    result = await reconcile(client=_feed_client(page_limit=2), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=1, budget_seconds=10)

    assert (result.behind, memory.seq) == (True, 3)


@pytest.mark.asyncio
@respx.mock
async def test_the_feed_lane_stamps_the_sequence_it_arrived_at() -> None:
    """The one lane that has a sequence number, so a stored row can say which door first told this
    subject about this run."""
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(9))
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


def _ingest_fast_then_stalling(fast_calls: int, stall_seconds: float) -> Callable[..., Awaitable[dict[str, str]]]:
    """An ingest that is FREE for `fast_calls`, then stalls far longer than any budget, so a pass that
    outruns its budget does so deterministically: neither half depends on how loaded the machine is."""
    calls = 0

    async def _ingest(*args: object, **kwargs: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls > fast_calls:
            await asyncio.sleep(stall_seconds)
        return DAPR_SUCCESS

    return _ingest


@pytest.mark.asyncio
@respx.mock
async def test_a_walk_cut_off_by_the_budget_keeps_what_it_handled_and_reports_its_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backlog deeper than one budget drains across ticks, because every handled row moves the mark. A
    gap the pass crossed before the timer fired is reported then: the saved mark is past it for good."""
    gaps: list[int] = []
    monkeypatch.setattr("notifications.api.reconciler.record_feed_gap", lambda value=1: gaps.append(value))
    monkeypatch.setattr("notifications.api.reconciler.ingest_run_event", _ingest_fast_then_stalling(fast_calls=3, stall_seconds=30))
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(*range(3, 50), oldest_seq=3))
    plane = _Plane()
    store, memory = _store(1)

    with pytest.raises(LineageFeedBudgetExceeded):
        await reconcile(client=_feed_client(page_limit=2), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=40, budget_seconds=0.5)

    assert memory.seq == 5, "an over-budget pass must record the rows it handled, and only those"
    assert gaps == [1], "the gap the pass crossed was saved past and never reported"


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
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(_watched_event(9, project="p1")))
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
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(9))
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


# --- a poison row must not block every newer notification ----------------------------------------
#
# The mark stops at a row that asks for a retry, which is right for a TRANSIENT failure. A PERMANENT one
# would hold it there forever and block every notification after it, for everyone, while each tick
# logged `lineage_feed_reconciled`. After `FEED_MAX_STALLS` passes on the same row, that row is stepped over.


@pytest.mark.asyncio
@respx.mock
async def test_a_permanently_failing_recipient_does_not_block_every_newer_notification() -> None:
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(9, _event(10, author="bob")))
    plane = _Plane(broken={"alice"})  # this subject's inbox refuses, every time, forever
    store, memory = _store(8)

    for _ in range(FEED_MAX_STALLS):
        await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)
    assert memory.seq == 9, "the poison row was not stepped over after FEED_MAX_STALLS passes"
    await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=5, budget_seconds=10)

    assert memory.seq == 10 and "bob" in plane.boxes, "the row after the poison row was never delivered"


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
# So the loss is detected on THIS side: lineage reports `oldest_seq`, and a mark below it means rows
# were deleted before this lane read them. That is the exact population the bus cannot serve — ingest
# and every external OpenLineage producer emit over HTTP only — so a silent loss here is the one that
# matters most.


@pytest.mark.asyncio
@respx.mock
async def test_a_feed_that_still_HOLDS_the_cursor_reports_no_gap() -> None:
    """The ordinary case, and the one a false positive would ruin: the mark is inside the retained
    window, so nothing was lost and the pass must not cry wolf every tick."""
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(9000, oldest_seq=1))
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
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(9000))
    plane = _Plane()
    store, _memory = _store(5)

    result = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=40, budget_seconds=10)

    assert result.gapped is False


@pytest.mark.asyncio
@respx.mock
async def test_a_gap_still_lets_the_pass_deliver_and_advance() -> None:
    """A gap is a REPORT, not a stall. The rows below the floor are gone whatever this pass does, so
    holding the mark would forfeit every row above them too."""
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(9000, oldest_seq=8999))
    plane = _Plane()
    store, memory = _store(5)

    result = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=40, budget_seconds=10)

    assert result.gapped is True
    assert result.scanned == 1, "the row that DID survive must still be delivered"
    assert memory.seq == 9000, "the mark must advance past a loss it cannot undo"


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
# BOTH unrecoverable conditions above are REPORTS, and a report is something you make once. Measured
# on the live estate 2026-08-30: one pruned feed and one malformed row produced 1 210 identical ERROR
# pairs over ten hours — two every thirty seconds, from the cron tick re-walking an IDLE feed. The
# mark cannot climb away from either condition when no new rows arrive, so each tick re-derives the
# same loss and re-announces it. Nothing was wrong with the diagnosis; it was the repetition that
# made `record_feed_gap()` count one gap 1 210 times and buried the estate's real errors.


@pytest.mark.asyncio
@respx.mock
async def test_the_SAME_prune_is_reported_ONCE_not_on_every_tick() -> None:
    """A gap is unrecoverable, so the second tick over an unchanged feed has nothing new to say. The
    mark moves past the hole when it is found, which is what makes the report a one-shot; a LATER prune
    raises `oldest_seq` past the new mark and is detected on its own terms."""
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(9000, oldest_seq=8999))
    plane = _Plane()
    store, memory = _store(5)

    first = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=40, budget_seconds=10)
    second = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=40, budget_seconds=10)

    assert first.gapped is True, "the prune must be reported the first time it comes into view"
    assert second.gapped is False, "the same prune, re-reported: this is the 1 210-errors-in-ten-hours bug"
    assert memory.seq == 9000


@pytest.mark.asyncio
@respx.mock
async def test_a_LATER_prune_is_still_reported_after_an_earlier_one_was_accepted() -> None:
    """The other half of the same contract: accepting a gap must not deafen the detector. If it did,
    the fix for the noise would silently cost the estate every future report of real data loss."""
    plane = _Plane()
    store, _memory = _store(5)
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(9000, oldest_seq=8999))
    await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=40, budget_seconds=10)

    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward(20000, oldest_seq=19999))
    later = await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=40, budget_seconds=10)

    assert later.gapped is True, "retention moved again and deleted rows 9001..19998 unread"


@pytest.mark.asyncio
@respx.mock
async def test_a_malformed_row_is_reported_once_and_the_mark_moves_past_it(caplog: pytest.LogCaptureFixture) -> None:
    """A malformed payload is DROPPED, not retried: redelivery cannot make it parse. So the mark moves past
    it and an idle feed does not report the same dead row on every tick."""
    bad = {"eventType": "FAIL", "seq": 101}  # no `run`, no `job` — cannot be a run event
    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_forward({"seq": 101, "event": bad}, oldest_seq=1))
    plane = _Plane()
    store, memory = _store(100)

    with caplog.at_level(logging.ERROR, logger="notifications.api.ingest"):
        await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=40, budget_seconds=10)
        await reconcile(client=_feed_client(), store=store, visibility=OPEN, open_inbox=plane.open, max_pages=40, budget_seconds=10)

    assert len([r for r in caplog.records if r.getMessage() == "lineage_event_invalid"]) == 1
    assert memory.seq == 101
