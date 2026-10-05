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
import logging
from typing import Any, cast

import httpx
import pytest
import respx
from fastapi import FastAPI

from notifications.api import subscriptions as subscriptions_module
from notifications.api.ingest import DAPR_SUCCESS, ingest_run_event
from notifications.api.metrics import Lane
from notifications.api.reconciler import LineageCursor, LineageCursorStore, LineageFeedBudgetExceeded, LineageFeedClient, reconcile
from notifications.api.settings import IngressSettings, get_ingress_settings
from notifications.api.visibility import Visibility
from notifications.proxies import TypedActorProxy
from service_kit.lakehouse.ns_errors import install_problem_handlers


LINEAGE = "http://lineage.invalid"
OPEN = Visibility(client=None, enabled=False)
pytestmark = pytest.mark.usefixtures("lineage_identity_token")


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
    install_problem_handlers(app, logging.getLogger(__name__))
    try:
        subscriptions_module.register_subscriptions(app)
        assert "/lineage-events" not in {getattr(route, "path", "") for route in app.routes}
    finally:
        get_ingress_settings.cache_clear()


# --- hostile payloads -------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("author", [""])
async def test_an_author_facet_that_is_not_a_verified_string_names_nobody(author: object, plane: _Plane) -> None:
    """Targeting reads `author.sub` and nothing else, and every non-string shape has to cost a
    notification rather than an exception inside a subscription handler."""
    event = _event()
    event["run"]["facets"]["author"]["sub"] = author

    assert await ingest_run_event(event, lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open) == DAPR_SUCCESS
    assert plane.boxes == {}


# --- the second lane: the reconciler ------------------------------------------------------------


class _MemoryCursor:
    def __init__(self, seq: int | None) -> None:
        self.seq = seq
        self.writes: list[int] = []

    async def get(self) -> LineageCursor | None:
        return None if self.seq is None else LineageCursor(seq=self.seq, updated_at="2026-08-09T12:00:00Z")

    async def set(self, seq: int, *, resume_from: int | None = None, pending_high: int | None = None, floor: int | None = None, stalls: int = 0) -> None:
        self.seq = seq
        self.writes.append(seq)


def _feed_client(*, page_limit: int = 500, timeout_seconds: float = 5.0) -> LineageFeedClient:
    return LineageFeedClient(
        client=httpx.AsyncClient(),
        base_url=LINEAGE,
        token_file=IngressSettings().lineage_identity_token_file,
        timeout_seconds=timeout_seconds,
        page_limit=page_limit,
    )


def _row(seq: int) -> dict[str, Any]:
    return {"seq": seq, "event": _event(run_id=f"run-{seq}")}


@pytest.mark.asyncio
@respx.mock
async def test_a_row_whose_event_is_garbage_is_dropped_and_the_walk_carries_on(plane: _Plane) -> None:
    """The contrast that makes the test above a gap rather than a preference: when the ROW parses, a
    payload that will never parse costs exactly that one notification."""
    respx.get(f"{LINEAGE}/events/projection").mock(
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
async def test_a_lineage_that_answers_slowly_fails_this_tick_instead_of_stacking_the_next_one(plane: _Plane, respx_allows_unused_routes) -> None:
    """The hard budget is `asyncio.timeout`, and what it protects is the CRON: a tick that outlives its
    period is a tick the next delivery lands on top of. The cursor stays put, so nothing is lost."""

    async def _slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"events": [_row(9)], "next_cursor": 9})

    respx.get(f"{LINEAGE}/events/projection").mock(side_effect=_slow)
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
    route = respx.get(f"{LINEAGE}/events/projection").mock(return_value=httpx.Response(200, json={"events": [_row(9), _row(8)], "next_cursor": 9}))
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
