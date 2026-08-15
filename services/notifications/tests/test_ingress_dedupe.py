"""One run, two doors, one pointer — and the bus subscription driven as a real route.

The design's whole reason for a second ingress is that the bus is provably incomplete: the ingest
service, Ray TRAIN and every external OpenLineage producer reach lineage's durable feed and never the
topic. That only works if a run arriving on BOTH lanes is not announced twice — so this suite sends
one event down each lane, in both orders, and counts the rows that land.

The subscription is ALSO exercised as a real HTTP route here, because two of the things that can go
wrong in it are invisible to a direct function call: the payload parameter being read as a QUERY
parameter (which 422s every delivery while the subscription looks healthy), and `body["data"]` never
being unwrapped (which DROPs every delivery — silently, since a DROP is an ack).
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from notifications.api import subscriptions as subscriptions_module
from notifications.api.ingest import DAPR_SUCCESS, ingest_run_event
from notifications.api.metrics import Lane
from notifications.api.reconciler import LineageCursor, LineageCursorStore, LineageFeedClient, reconcile
from notifications.api.settings import get_ingress_settings
from notifications.api.visibility import Visibility
from notifications.config import get_notifications_settings
from notifications.proxies import TypedActorProxy


LINEAGE = "http://lineage.invalid"
OPEN = Visibility(client=None, enabled=False)

RUN_EVENT: dict[str, Any] = {
    "eventType": "FAIL",
    "eventTime": "2026-08-09T12:00:00+00:00",
    "run": {"runId": "run-77", "facets": {"author": {"name": "alice", "sub": "alice"}, "lance": {"run_id": "ingest-77"}}},
    "job": {"namespace": "lance-ingest", "name": "harvest"},
    "outputs": [{"namespace": "bronze", "name": "bronze$pages"}],
}


class _Inbox:
    def __init__(self, plane: "_Plane", subject: str) -> None:
        self._plane = plane
        self._subject = subject

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = self._plane.boxes.setdefault(self._subject, [])
        if any(row["notification_id"] == payload["notification_id"] for row in rows):
            return {"delivered": False, "unread": len(rows), "rows": len(rows)}
        rows.append(payload)
        return {"delivered": True, "unread": len(rows), "rows": len(rows)}


class _Plane:
    def __init__(self) -> None:
        self.boxes: dict[str, list[dict[str, Any]]] = {}

    def open(self, subject: str) -> TypedActorProxy:
        return cast(TypedActorProxy, _Inbox(self, subject))


class _MemoryCursor:
    def __init__(self, seq: int) -> None:
        self.seq = seq

    async def get(self) -> LineageCursor | None:
        return LineageCursor(seq=self.seq, updated_at=datetime.now(UTC))

    async def set(self, seq: int, *, resume_from: int | None = None, pending_high: int | None = None, floor: int | None = None) -> None:
        self.seq = seq


@pytest.fixture
def plane() -> _Plane:
    return _Plane()


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch, plane: _Plane) -> Iterator[TestClient]:
    get_ingress_settings.cache_clear()
    app = FastAPI()
    app.state.notifications_settings = get_notifications_settings()
    app.state.fga = None
    subscriptions_module.register_subscriptions(app)
    monkeypatch.setattr(subscriptions_module, "inbox_for", plane.open)
    with TestClient(app) as client:
        yield client
    get_ingress_settings.cache_clear()


def _cloud_event(event: dict[str, Any]) -> dict[str, Any]:
    """The envelope Dapr delivers: the OpenLineage event sits under `data`."""
    return {"id": "ce-1", "source": "lineage", "type": "com.dapr.event.sent", "topic": "lineage.events.v1", "data": event}


def test_the_subscription_is_advertised_to_the_sidecar(bus: TestClient) -> None:
    """`GET /dapr/subscribe` is the registration daprd reads at startup — the wiring is inspectable
    even where no broker exists."""
    declared = bus.get("/dapr/subscribe").json()
    assert [(entry["pubsubname"], entry["topic"], entry["route"]) for entry in declared] == [
        ("lineage-pubsub-notifications", "lineage.events.v1", "/lineage-events"),
        # v3 targeting, on its OWN component: `queueGroupName` lives on the component, and adding a
        # scope to the catalog's BROADCAST component would split an every-replica broadcast into a
        # competing-consumer group instead of joining it.
        ("catalog-control-pubsub-notifications", "catalog.control.v1", "/control-events"),
    ]


def test_a_delivered_cloud_event_is_a_body_not_a_query_parameter(bus: TestClient, plane: _Plane) -> None:
    """A handler whose payload parameter is typed bare `Any` becomes a QUERY parameter, and every
    delivery then answers `422 {"field": "query.event"}` while the subscription looks healthy."""
    response = bus.post("/lineage-events", json=_cloud_event(RUN_EVENT))
    assert response.status_code == 200
    assert response.json() == {"status": "SUCCESS"}
    assert len(plane.boxes["alice"]) == 1


def test_the_handler_unwraps_the_envelope_rather_than_treating_it_as_the_event(bus: TestClient, plane: _Plane) -> None:
    """The CloudEvent's own fields are not the run's. Reading the envelope as the payload would DROP
    every delivery — silently, since a DROP is an ack."""
    bus.post("/lineage-events", json=_cloud_event(RUN_EVENT))
    stored = plane.boxes["alice"][0]
    assert stored["notification_id"] == "run-77@FAIL"
    assert stored["source_run_id"] == "ingest-77"
    assert stored["object_id"] == "bronze$pages"


def test_an_envelope_with_no_event_is_dropped(bus: TestClient, plane: _Plane) -> None:
    assert bus.post("/lineage-events", json={"id": "ce-2"}).json() == {"status": "DROP"}
    assert plane.boxes == {}


def test_a_pointer_from_the_bus_carries_no_feed_sequence(bus: TestClient, plane: _Plane) -> None:
    """`event_seq` is the FEED's number. A bus row that claimed one would be asserting where it came
    from, wrongly."""
    bus.post("/lineage-events", json=_cloud_event(RUN_EVENT))
    assert plane.boxes["alice"][0]["event_seq"] is None


async def _feed_tick(plane: _Plane, *, seq: int, cursor: int) -> Any:
    """One reconciler pass over a feed holding exactly the run above."""
    respx.get(f"{LINEAGE}/events").mock(return_value=httpx.Response(200, json={"events": [{"seq": seq, "event": RUN_EVENT}], "next_cursor": None}))
    feed = LineageFeedClient(
        client=httpx.AsyncClient(),
        base_url=LINEAGE,
        identity="notifications",
        token=SecretStr("app-token"),
        timeout_seconds=5.0,
        page_limit=500,
    )
    return await reconcile(
        client=feed,
        store=cast(LineageCursorStore, _MemoryCursor(cursor)),
        visibility=OPEN,
        open_inbox=plane.open,
        max_pages=5,
        budget_seconds=10,
    )


async def _bus_delivery(plane: _Plane) -> dict[str, str]:
    """The bus lane, entered where its route enters it: the unwrapped `data`.

    Called directly rather than through the route here, because this suite's async half drives the
    reconciler's HTTPX egress under respx and the route's test client is a synchronous transport of
    its own — mixing the two would test the harness. The route's own shape (body-not-query, the
    envelope unwrap) is pinned by the tests above, which drive it as HTTP.
    """
    return await ingest_run_event(RUN_EVENT, lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)


@pytest.mark.asyncio
@respx.mock
async def test_the_same_run_arriving_on_both_lanes_lands_exactly_one_pointer(plane: _Plane) -> None:
    """The property the two-ingress design rests on. It holds because the notification id IS lineage's
    own terminal natural key — `(run_id, event_type)` — so the two lanes cannot disagree about what
    "the same notification" is, and the actor is idempotent on it."""
    assert await _bus_delivery(plane) == DAPR_SUCCESS
    assert len(plane.boxes["alice"]) == 1

    result = await _feed_tick(plane, seq=12, cursor=11)

    assert result.scanned == 1
    assert len(plane.boxes["alice"]) == 1


@pytest.mark.asyncio
@respx.mock
async def test_the_feed_reaching_it_first_is_the_same_story(plane: _Plane) -> None:
    """Order must not matter: the HTTP-only lanes are exactly the ones the bus never carries, so which
    door sees a run first is a property of the producer, not of this plane."""
    await _feed_tick(plane, seq=12, cursor=11)
    assert plane.boxes["alice"][0]["event_seq"] == 12

    assert await _bus_delivery(plane) == DAPR_SUCCESS
    assert len(plane.boxes["alice"]) == 1
