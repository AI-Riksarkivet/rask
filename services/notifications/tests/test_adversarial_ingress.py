"""The ingress under attack: a hostile publisher, a hostile payload, and a lane that must not livelock.

This suite is written from the OTHER side of the bus. Everything it sends is something a producer could
send — the run id, the author facet, the output names and the event time are all attacker-influenced on
an ungoverned topic — and each test names what the plane is supposed to do about it and, where it does
not, says so in its own name rather than in a comment nobody reads.

Three of these tests pin a GAP rather than a guarantee. They are named for the gap, they pass today, and
the day the gap is closed they fail and are rewritten — which is the point: a gap that no test mentions
is indistinguishable from a decision, and this plane has already paid once for exactly that confusion.
The one gap serious enough to be a defect rather than a bound carries `xfail(strict=True)` instead, so
closing it turns the suite red until the marker goes with it.
"""

import asyncio
import json
from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from notifications.api import metrics as metrics_module
from notifications.api import subscriptions as subscriptions_module
from notifications.api.ingest import DAPR_DROP, DAPR_RETRY, DAPR_SUCCESS, ingest_run_event
from notifications.api.metrics import Lane
from notifications.api.reconciler import LineageCursor, LineageCursorStore, LineageFeedBudgetExceeded, LineageFeedClient, reconcile
from notifications.api.settings import get_ingress_settings
from notifications.api.visibility import Visibility
from notifications.config import get_notifications_settings
from notifications.proxies import TypedActorProxy, inbox_actor_id


LINEAGE = "http://lineage.invalid"
OPEN = Visibility(client=None, enabled=False)
APP_TOKEN = "an-app-token"

#: One mebibyte of a single character — the shape a producer's runaway id or dataset name takes.
HUGE = "R" * (1024 * 1024)


def _event(
    *,
    event_type: str = "FAIL",
    run_id: str = "run-1",
    author: str | None = "alice",
    facets: dict[str, Any] | None = None,
    outputs: list[str] | None = None,
    event_time: str = "2026-08-09T12:00:00+00:00",
) -> dict[str, Any]:
    bag: dict[str, Any] = {} if author is None else {"author": {"name": author, "sub": author}}
    if facets is not None:
        bag.update(facets)
    return {
        "eventType": event_type,
        "eventTime": event_time,
        "run": {"runId": run_id, "facets": bag},
        "outputs": [{"namespace": "silver", "name": name} for name in (outputs if outputs is not None else ["silver$pages"])],
    }


class _Inbox:
    """One subject's inbox with the real actor's idempotency contract, and an optional injected fault."""

    def __init__(self, plane: "_Plane", subject: str) -> None:
        self._plane = plane
        self._subject = subject

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._plane.fault is not None:
            raise self._plane.fault
        rows = self._plane.boxes.setdefault(self._subject, [])
        if any(row["notification_id"] == payload["notification_id"] for row in rows):
            return {"delivered": False, "unread": len(rows), "rows": len(rows)}
        rows.append(payload)
        return {"delivered": True, "unread": len(rows), "rows": len(rows)}


class _Plane:
    """A whole actor plane in a dict, addressed the way `inbox_for` addresses the real one."""

    def __init__(self, fault: Exception | None = None) -> None:
        self.boxes: dict[str, list[dict[str, Any]]] = {}
        self.fault = fault

    def open(self, subject: str) -> TypedActorProxy:
        return cast(TypedActorProxy, _Inbox(self, subject))


@pytest.fixture
def plane() -> _Plane:
    return _Plane()


# --- who may push into an inbox --------------------------------------------------------------


@pytest.fixture
def authenticated_bus(monkeypatch: pytest.MonkeyPatch, plane: _Plane) -> Iterator[TestClient]:
    """The subscription as a DEPLOYED one: Dapr ingest on, an app token set, a DLQ topic configured.

    The suite's other bus fixture (`test_ingress_dedupe.py`) drives the dev shape, where the token guard
    is a documented no-op. This one is the shape that is actually exposed to a cluster, and it is the
    only one in which the guard can be observed refusing anything.
    """
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.setenv("APP_API_TOKEN", APP_TOKEN)
    monkeypatch.setenv("RASK_NOTIFICATIONS_DLQ_TOPIC", "dlq.notifications")
    get_ingress_settings.cache_clear()
    app = FastAPI()
    app.state.notifications_settings = get_notifications_settings()
    app.state.fga = None
    subscriptions_module.register_subscriptions(app)
    monkeypatch.setattr(subscriptions_module, "inbox_for", plane.open)
    with TestClient(app) as client:
        yield client
    get_ingress_settings.cache_clear()


def _envelope(event: dict[str, Any]) -> dict[str, Any]:
    return {"id": "ce-1", "source": "lineage", "type": "com.dapr.event.sent", "topic": "lineage.events.v1", "data": event}


def test_a_delivery_with_no_app_token_is_refused(authenticated_bus: TestClient, plane: _Plane) -> None:
    """The route is delivered by this app's own sidecar, which stamps the token on every request. A
    delivery without it did not come from the sidecar."""
    assert authenticated_bus.post("/lineage-events", json=_envelope(_event())).status_code == 403
    assert plane.boxes == {}


def test_a_delivery_with_the_wrong_app_token_is_refused(authenticated_bus: TestClient, plane: _Plane) -> None:
    assert authenticated_bus.post("/lineage-events", json=_envelope(_event()), headers={"dapr-api-token": "not-it"}).status_code == 403
    assert plane.boxes == {}


def test_a_delivery_through_a_public_front_door_is_refused_even_holding_a_valid_token(authenticated_bus: TestClient, plane: _Plane) -> None:
    """The measured bypass: the gateway forwards `/api/*` through Dapr service invocation, so an
    anonymous public request arrives at a backend already holding a valid service token. The token
    proves "arrived via Dapr", never "the caller is trusted"."""
    response = authenticated_bus.post(
        "/lineage-events",
        json=_envelope(_event()),
        headers={"dapr-api-token": APP_TOKEN, "dapr-caller-app-id": "gateway"},
    )
    assert response.status_code == 403
    assert plane.boxes == {}


def test_the_sidecars_own_delivery_is_accepted(authenticated_bus: TestClient, plane: _Plane) -> None:
    """The positive half — without it the three refusals above would also pass against a broken route."""
    response = authenticated_bus.post("/lineage-events", json=_envelope(_event()), headers={"dapr-api-token": APP_TOKEN})
    assert response.json() == {"status": "SUCCESS"}
    assert len(plane.boxes["alice"]) == 1


def test_enabling_dapr_ingest_without_an_app_token_refuses_to_build_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed at build time. An unauthenticated ingest path that LOOKS configured is the one
    outcome that cannot be noticed, so the pod refuses to start instead."""
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    get_ingress_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="APP_API_TOKEN"):
            subscriptions_module.register_subscriptions(FastAPI())
    finally:
        get_ingress_settings.cache_clear()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP: register_subscriptions mounts POST /lineage-events unconditionally, so with Dapr ingest off "
        "(the default) the route is live AND unauthenticated — assert_app_token_configured no-ops and "
        "require_dapr_token no-ops without APP_API_TOKEN — and any caller that can reach :8850 can put a "
        "pointer in a named subject's inbox. lineage and catalog both gate the subscribe() call on their "
        "own enabled flag for exactly this reason ('an HTTP-only deployment carries no always-live ingest "
        "route'). Unblocks when subscriptions.register_subscriptions wraps the subscribe() calls in "
        "`if settings.dapr_enabled:`; delete this marker with the fix."
    ),
)
def test_no_ingest_route_exists_when_dapr_ingest_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_DAPR_ENABLED", "false")
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    get_ingress_settings.cache_clear()
    app = FastAPI()
    try:
        subscriptions_module.register_subscriptions(app)
        assert "/lineage-events" not in {getattr(route, "path", "") for route in app.routes}
    finally:
        get_ingress_settings.cache_clear()


# --- hostile payloads -------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("a bare list", [1, 2, 3]),
        ("a run with no id", {"eventType": "FAIL", "eventTime": "2026-08-09T12:00:00+00:00", "run": {}}),
        ("outputs that are not datasets", {"eventType": "FAIL", "eventTime": "2026-08-09T12:00:00+00:00", "run": {"runId": "r"}, "outputs": "silver"}),
        ("a facet bag that is a list", {"eventType": "FAIL", "eventTime": "2026-08-09T12:00:00+00:00", "run": {"runId": "r", "facets": []}}),
        ("an event time that is an object", {"eventType": "FAIL", "eventTime": {"when": "now"}, "run": {"runId": "r"}}),
        ("an event type that is empty", {"eventType": "", "eventTime": "2026-08-09T12:00:00+00:00", "run": {"runId": "r"}}),
    ],
)
async def test_a_structurally_hostile_payload_is_dropped_never_retried(label: str, payload: object, plane: _Plane) -> None:
    """DROP is the only answer redelivery cannot make worse. A RETRY here returns the same message
    forever and everything queued behind it waits — the poisoned subscription."""
    assert await ingest_run_event(payload, lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open) == DAPR_DROP, label
    assert plane.boxes == {}


@pytest.mark.asyncio
async def test_a_deeply_nested_facet_bag_does_not_raise_inside_the_handler(plane: _Plane) -> None:
    """`facets` is `dict[str, Any]`, and `Any` is not traversed — so nesting costs no stack. It is
    asserted rather than assumed because a `RecursionError` is NOT in the handler's caught set: it would
    escape as a 500, which the sidecar reads as RETRY, which is the poisoned subscription again."""
    deep: dict[str, Any] = {}
    node = deep
    for _ in range(20_000):
        node["a"] = {}
        node = node["a"]

    assert await ingest_run_event(_event(facets={"deep": deep}), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open) == DAPR_SUCCESS
    assert len(plane.boxes["alice"]) == 1


def test_a_duplicate_json_key_resolves_to_the_last_one(authenticated_bus: TestClient, plane: _Plane) -> None:
    """A hand-rolled envelope can repeat a key. There is exactly one reading of that (the parser's
    last-wins), and what matters is that the FIRST value cannot be the one that decides the audience
    while a reviewer reads the second."""
    body = (
        '{"data": {"eventType": "START", "eventType": "FAIL", "eventTime": "2026-08-09T12:00:00+00:00",'
        '"run": {"runId": "dup", "facets": {"author": {"sub": "alice"}}}, "outputs": [{"name": "silver$pages"}]}}'
    )
    response = authenticated_bus.post(
        "/lineage-events",
        content=body,
        headers={"dapr-api-token": APP_TOKEN, "content-type": "application/json"},
    )

    assert response.json() == {"status": "SUCCESS"}
    assert [row["notification_id"] for row in plane.boxes["alice"]] == ["dup@FAIL"]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", ["a||b", "../../etc/passwd", "x\x00y", "run/with/slashes", "run-1@FAIL"])
async def test_a_hostile_run_id_never_reaches_a_key_or_an_id_it_could_break(run_id: str, plane: _Plane) -> None:
    """Dapr's reserved `||`, a traversal, a NUL and a slash all survive into the stored notification id —
    and that is harmless BECAUSE the id is never a key, a path segment or an actor address. The one
    thing derived from a caller-influenced string is the ACTOR ID, and it comes from the subject alone.
    This test is the standing proof of that separation, not of any sanitising."""
    await ingest_run_event(_event(run_id=run_id), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)
    stored = plane.boxes["alice"][0]["notification_id"]

    assert stored == f"{run_id}@FAIL"
    assert "||" not in inbox_actor_id("alice")
    assert stored not in inbox_actor_id("alice")


@pytest.mark.asyncio
async def test_a_run_whose_id_already_ends_in_a_state_cannot_collide_with_another_runs_notification(plane: _Plane) -> None:
    """`run_id@STATE` parses from the RIGHT and the state comes from a closed set, so the scheme stays
    injective however much of it a producer imitates in its own run id."""
    await ingest_run_event(_event(run_id="run-1@FAIL", event_type="FAIL"), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)
    await ingest_run_event(_event(run_id="run-1", event_type="FAIL"), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)

    assert sorted(row["notification_id"] for row in plane.boxes["alice"]) == ["run-1@FAIL", "run-1@FAIL@FAIL"]


@pytest.mark.asyncio
@pytest.mark.parametrize("author", [7, 0.5, True, None, "", "   ", [], {"sub": "alice"}])
async def test_an_author_facet_that_is_not_a_verified_string_names_nobody(author: object, plane: _Plane) -> None:
    """Targeting reads `author.sub` and nothing else, and every non-string shape has to cost a
    notification rather than an exception inside a subscription handler."""
    event = _event()
    event["run"]["facets"]["author"]["sub"] = author

    assert await ingest_run_event(event, lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open) == DAPR_SUCCESS
    assert plane.boxes == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_time", "expected_year"),
    [(0, 1970), (0.5, 1970), (4_102_444_800, 2100), ("1970-01-01T00:00:00Z", 1970), ("9999-12-31T23:59:59Z", 9999)],
)
async def test_the_instant_a_pointer_is_ordered_by_is_whatever_the_producer_said(event_time: object, expected_year: int, plane: _Plane) -> None:
    """GAP, pinned, and the input to the eviction case in `test_adversarial_inbox.py`.

    `eventTime` is parsed by pydantic, which reads a NUMBER as a Unix epoch — so a producer states the
    instant absolutely, in two notations, with no clock-skew bound anywhere between the topic and the
    feed's sort key. A row stamped in the past is compacted away on the next tick; a row stamped in the
    future pins the top of the panel and survives every compaction. Neither is a parse error, and the
    plane has no opinion about either.

    Rewrite when the projection bounds `occurred_at` against the receiver's own clock.
    """
    await ingest_run_event(_event(event_time=cast(str, event_time)), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)

    assert plane.boxes["alice"][0]["occurred_at"].startswith(str(expected_year))


@pytest.mark.asyncio
async def test_no_length_bound_stands_between_a_bus_payload_and_a_stored_pointer(plane: _Plane) -> None:
    """GAP, pinned so it is a decision rather than an oversight.

    The claim-check invariant says a pointer names a notification and carries no payload. It holds for
    the SHAPE — no error text, no dataset contents — and not at all for SIZE: `notification_id`,
    `object_id` and `source_run_id` are producer strings with no `max_length` anywhere between the topic
    and the actor's state blob, and the 900 KiB claim-check guard lives on `dapr_publish.publish_event`,
    which is on the emitting side and covers nothing this service reads.

    So a producer with a runaway id writes megabytes into a named subject's inbox row, and the actor
    rewrites the whole blob on every subsequent delivery. Under FGA the `object_id` still has to be a
    table the recipient may read, which bounds WHOSE inbox — it bounds nothing about the size.

    Rewrite this test when a bound lands; do not delete it.
    """
    await ingest_run_event(_event(run_id=HUGE, outputs=[HUGE]), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)
    stored = plane.boxes["alice"][0]

    assert len(stored["notification_id"]) > 1_000_000
    assert len(stored["object_id"]) > 1_000_000


@pytest.mark.asyncio
async def test_no_length_bound_stands_between_a_bus_payload_and_an_actor_id(plane: _Plane) -> None:
    """The same gap where it bites hardest. The author facet becomes an actor id, and an actor id travels
    in the sidecar's URL PATH — so a runaway author is not a large row, it is a call the sidecar cannot
    make, classified as a transient failure (below) and therefore redelivered forever."""
    assert len(inbox_actor_id(HUGE)) > 1_000_000


# --- replay, redelivery and ordering ------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_concurrent_copies_of_one_event_land_one_pointer(plane: _Plane) -> None:
    """At-least-once means the same message can be in flight to two replicas at once. Every copy must
    ack, and exactly one row may exist afterwards — the guard is the natural key, not a window."""
    statuses = await asyncio.gather(*(ingest_run_event(_event(), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open) for _ in range(3)))

    assert statuses == [DAPR_SUCCESS, DAPR_SUCCESS, DAPR_SUCCESS]
    assert len(plane.boxes["alice"]) == 1


@pytest.mark.asyncio
async def test_a_start_arriving_after_its_terminal_state_adds_nothing(plane: _Plane) -> None:
    """Out-of-order delivery is the bus's normal state. It cannot matter here, because the only events
    that reach an inbox are terminal ones and a START is dropped on its own merits."""
    await ingest_run_event(_event(event_type="FAIL"), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)

    assert await ingest_run_event(_event(event_type="START"), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open) == DAPR_SUCCESS
    assert len(plane.boxes["alice"]) == 1


@pytest.mark.asyncio
async def test_one_run_reaching_two_terminal_states_is_two_notifications(plane: _Plane) -> None:
    """A run cannot honestly be both, but a producer can say so — and the answer is two rows rather than
    a silent overwrite, because dismissing one must not silence the other."""
    await ingest_run_event(_event(event_type="COMPLETE"), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)
    await ingest_run_event(_event(event_type="ABORT"), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)

    assert sorted(row["notification_id"] for row in plane.boxes["alice"]) == ["run-1@ABORT", "run-1@COMPLETE"]


@pytest.mark.asyncio
async def test_a_permanent_recipient_failure_is_retried_forever_rather_than_dropped(plane: _Plane) -> None:
    """GAP, pinned.

    `_deliver_one` catches every exception into ONE outcome — `RETRIED` — so the ingress has no way to
    say "this recipient can never be delivered to". A recipient whose failure is permanent (the runaway
    actor id above is the concrete case: the sidecar cannot route a megabyte-long id, ever) therefore
    makes the event RETRY on every redelivery, and with `dlq_topic` empty by default there is no
    dead-letter under it to catch the loop.

    That is a livelock the DROP/RETRY vocabulary exists to prevent, arriving one layer below where the
    vocabulary is applied. Rewrite when the fan-out grows a permanent-failure outcome.
    """
    permanent = _Plane(fault=ValueError("an inbox requires a non-empty verified subject"))

    for _ in range(3):
        assert await ingest_run_event(_event(), lane=Lane.BUS, visibility=OPEN, open_inbox=permanent.open) == DAPR_RETRY


# --- the second lane: the reconciler ------------------------------------------------------------


class _MemoryCursor:
    def __init__(self, seq: int | None) -> None:
        self.seq = seq
        self.writes: list[int] = []

    async def get(self) -> LineageCursor | None:
        return None if self.seq is None else LineageCursor(seq=self.seq, updated_at="2026-08-09T12:00:00Z")

    async def set(self, seq: int) -> None:
        self.seq = seq
        self.writes.append(seq)


def _feed_client(*, page_limit: int = 500, timeout_seconds: float = 5.0) -> LineageFeedClient:
    return LineageFeedClient(
        client=httpx.AsyncClient(),
        base_url=LINEAGE,
        identity="notifications",
        token=SecretStr(APP_TOKEN),
        timeout_seconds=timeout_seconds,
        page_limit=page_limit,
    )


def _row(seq: int) -> dict[str, Any]:
    return {"seq": seq, "event": _event(run_id=f"run-{seq}")}


@pytest.mark.asyncio
@respx.mock
async def test_one_unparseable_row_stalls_the_whole_feed_lane(plane: _Plane) -> None:
    """GAP, pinned — and the sharpest asymmetry in the plane.

    The bus lane validates PER EVENT and drops what will never parse. The feed lane validates per PAGE
    (`FeedPage.model_validate(response.json())`), so a single row whose `seq` is not an integer makes the
    whole page unparseable, the walk raises, and the cursor never moves. Every later tick fetches the
    same page and fails the same way: the lane whose entire purpose is completeness stops, permanently,
    on one row, and nothing about the failure says which row.

    Rewrite when the walk tolerates a bad row the way the handler tolerates a bad event.
    """
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_row(9), {"seq": None, "event": {}}], "next_cursor": 8}))
    memory = _MemoryCursor(5)

    with pytest.raises(Exception, match="validation error"):
        await reconcile(
            client=_feed_client(),
            store=cast(LineageCursorStore, memory),
            visibility=OPEN,
            open_inbox=plane.open,
            max_pages=5,
            budget_seconds=10,
        )

    assert memory.writes == []
    assert plane.boxes == {}


@pytest.mark.asyncio
@respx.mock
async def test_a_row_whose_event_is_garbage_is_dropped_and_the_walk_carries_on(plane: _Plane) -> None:
    """The contrast that makes the test above a gap rather than a preference: when the ROW parses, a
    payload that will never parse costs exactly that one notification."""
    respx.get(f"{LINEAGE}/events").mock(
        return_value=httpx.Response(200, json={"events": [_row(9), {"seq": 8, "event": {"nonsense": True}}, _row(7)], "next_cursor": None})
    )
    memory = _MemoryCursor(5)

    result = await reconcile(
        client=_feed_client(),
        store=cast(LineageCursorStore, memory),
        visibility=OPEN,
        open_inbox=plane.open,
        max_pages=5,
        budget_seconds=10,
    )

    assert (result.scanned, result.retried) == (3, 0)
    assert len(plane.boxes["alice"]) == 2
    assert memory.seq == 9


@pytest.mark.asyncio
@respx.mock
async def test_a_lineage_that_answers_slowly_fails_this_tick_instead_of_stacking_the_next_one(plane: _Plane) -> None:
    """The hard budget is `asyncio.timeout`, and what it protects is the CRON: a tick that outlives its
    period is a tick the next delivery lands on top of. The cursor stays put, so nothing is lost."""

    async def _slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"events": [_row(9)], "next_cursor": 9})

    respx.get(f"{LINEAGE}/events").mock(side_effect=_slow)
    memory = _MemoryCursor(1)

    with pytest.raises(LineageFeedBudgetExceeded):
        await reconcile(
            client=_feed_client(),
            store=cast(LineageCursorStore, memory),
            visibility=OPEN,
            open_inbox=plane.open,
            max_pages=50,
            budget_seconds=0.05,
        )

    assert memory.writes == []


@pytest.mark.asyncio
@respx.mock
async def test_a_feed_whose_cursor_never_advances_is_bounded_by_the_page_budget(plane: _Plane) -> None:
    """A feed that keeps handing back the same `next_cursor` is a loop with no exit of its own. The page
    budget is the exit, and the walk says it truncated rather than pretending it finished."""
    route = respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_row(9), _row(8)], "next_cursor": 9}))
    memory = _MemoryCursor(1)

    result = await reconcile(
        client=_feed_client(page_limit=2),
        store=cast(LineageCursorStore, memory),
        visibility=OPEN,
        open_inbox=plane.open,
        max_pages=3,
        budget_seconds=10,
    )

    assert route.call_count == 3
    assert result.truncated


@pytest.mark.asyncio
@respx.mock
async def test_the_feed_lane_re_offering_a_run_the_bus_already_delivered_changes_nothing(plane: _Plane) -> None:
    """The two-lane design's load-bearing property, driven from the direction an attacker would: the
    feed is re-walked after a cursor rollback, so every row above the mark is offered again."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [_row(9)], "next_cursor": None}))
    await ingest_run_event(_event(run_id="run-9"), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)

    for _ in range(3):
        await reconcile(
            client=_feed_client(),
            store=cast(LineageCursorStore, _MemoryCursor(8)),
            visibility=OPEN,
            open_inbox=plane.open,
            max_pages=5,
            budget_seconds=10,
        )

    assert len(plane.boxes["alice"]) == 1


# --- the dead-letter parking lot ----------------------------------------------------------------


def test_the_parking_route_declares_no_dead_letter_of_its_own(authenticated_bus: TestClient) -> None:
    """A DLQ subscription that dead-letters is a loop onto the topic whose whole purpose is to be the end
    of the line, with nothing under it."""
    declared = {entry["topic"]: entry for entry in authenticated_bus.get("/dapr/subscribe").json()}

    assert declared["lineage.events.v1"]["deadLetterTopic"] == "dlq.notifications"
    assert "deadLetterTopic" not in declared["dlq.notifications"]
    assert declared["dlq.notifications"]["route"] == "/dlq-event"


@pytest.mark.parametrize("data", [None, "a string", 7, [], {"run": "not-a-dict"}, {"run": {"runId": None}}])
def test_a_malformed_dead_letter_is_still_acked(authenticated_bus: TestClient, data: object) -> None:
    """A parked payload is BY DEFINITION one something already failed to handle, so the parking lot must
    read it defensively and ack regardless. Returning RETRY from here re-queues a message that has
    already exhausted a full retry schedule."""
    response = authenticated_bus.post("/dlq-event", json={"id": "ce-9", "data": data}, headers={"dapr-api-token": APP_TOKEN})

    assert response.json() == {"status": "SUCCESS"}


def test_the_parking_route_is_authenticated_like_the_ingest_route(authenticated_bus: TestClient) -> None:
    assert authenticated_bus.post("/dlq-event", json={"id": "ce-9"}).status_code == 403


# --- the claim-check invariant, at the one place it is a security rule ---------------------------


class _Counter:
    """Stands in for one OTel counter and records every label set it is handed."""

    def __init__(self) -> None:
        self.attributes: list[dict[str, str]] = []

    def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
        self.attributes.append(dict(attributes or {}))


@pytest.mark.asyncio
async def test_no_metric_label_can_carry_a_subject_an_object_or_a_run(monkeypatch: pytest.MonkeyPatch, plane: _Plane) -> None:
    """Bounded cardinality is a SECURITY rule here, not a cost rule: a counter labelled by subject
    publishes the estate's user list into a store nothing authorizes reads against.

    The counters are swapped rather than a global `MeterProvider` installed, because a MeterProvider is
    process-global and may be set once — a test that claimed it would silently change what every other
    suite in the session measures.
    """
    counters = {name: _Counter() for name in ("_ingress_events", "_fanout_recipients", "_dlq_parked")}
    for name, counter in counters.items():
        monkeypatch.setattr(metrics_module, name, counter)

    canary = "alice-canary-subject"
    await ingest_run_event(
        _event(run_id="run-canary", author=canary, outputs=["gold$canary-object"], facets={"lance": {"run_id": "src-canary"}}),
        lane=Lane.BUS,
        visibility=OPEN,
        open_inbox=plane.open,
    )
    metrics_module.record_dead_letter("notifications")

    recorded = [labels for counter in counters.values() for labels in counter.attributes]
    assert recorded, "the ingress recorded nothing — the assertion below would be vacuous"
    flattened = json.dumps(recorded)
    for secret in (canary, "gold$canary-object", "run-canary", "src-canary"):
        assert secret not in flattened
    assert {key for labels in recorded for key in labels} == {
        "lance.notifications.lane",
        "lance.notifications.outcome",
        "lance.notifications.app",
    }


@pytest.mark.asyncio
async def test_a_stored_pointer_names_no_field_the_event_did_not_have_to_earn(plane: _Plane) -> None:
    """The claim-check invariant as a field-set assertion. A row is an id, a reason, ONE object name, a
    run link, a lane sequence and an instant — and adding anything read off the payload (an error
    message, a job name, a namespace) would make this store a second, ungoverned copy of lineage."""
    event = _event(facets={"lance": {"run_id": "ingest-7"}})
    event["job"] = {"namespace": "lance-medallion", "name": "htr"}
    event["run"]["facets"]["errorMessage"] = {"message": "boom", "stackTrace": "secrets"}

    await ingest_run_event(event, lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)

    assert set(plane.boxes["alice"][0]) == {"notification_id", "reason", "object_id", "source_run_id", "event_seq", "occurred_at"}
    assert "boom" not in json.dumps(plane.boxes["alice"][0])
