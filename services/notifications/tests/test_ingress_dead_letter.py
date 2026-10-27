"""The floor under RETRY: where a delivery goes when the sidecar's schedule is exhausted.

RETRY is only an honest answer if something eventually catches the message. Without a dead-letter
topic, `maxDeliver` is reached and the delivery ceases to exist — no row, no park, no counter. With
one, the sidecar publishes it here and acks the original, and this route is the parking lot's floor.

Three rules, each of which has already cost the estate something:

* **The default is OFF, and that is a safety default rather than a deferral.** A `deadLetterTopic`
  declared on a subscription whose app has no entry in the resiliency CRD has no retry policy to
  exhaust, so the FIRST transient RETRY parks the message — measured on the medallion publication
  head. Turning it on is one act with registering the app; the ordering is enforced by an operator,
  so the code's default has to be the safe one.
* **A DLQ handler acks unconditionally.** RETRY from here re-queues a message that has already failed
  a full schedule, onto the topic whose entire purpose is to be the end of the line.
* **The label is THIS app's id.** A shared per-subTopic DLQ once had two apps subscribed to each
  other's dead letters, each counting the other's parks; the counter then says something true about
  nobody.
"""

import asyncio
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications.api import metrics as metrics_module
from notifications.api import reconcile_cron
from notifications.api import subscriptions as subscriptions_module
from notifications.api.control_parks import INDEX_KEY, PARK_HORIZON, ControlParkStore, park_key
from notifications.api.reconciler import FeedPage, LineageCursor, LineageCursorStore, LineageFeedClient
from notifications.api.settings import get_ingress_settings
from notifications.config import get_notifications_settings
from service_kit.lakehouse.ns_errors import install_problem_handlers


DLQ_TOPIC = "dlq.notifications"

RUN_EVENT: dict[str, Any] = {
    "eventType": "FAIL",
    "eventTime": "2026-08-09T12:00:00+00:00",
    "run": {"runId": "run-77", "facets": {"author": {"name": "alice", "sub": "alice"}}},
    "outputs": [{"namespace": "bronze", "name": "bronze$pages"}],
}


class _Counter:
    """Stands in for the `notifications.dlq.parked` instrument."""

    def __init__(self) -> None:
        self.adds: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
        self.adds.append((amount, dict(attributes or {})))


@pytest.fixture
def parked(monkeypatch: pytest.MonkeyPatch) -> _Counter:
    counter = _Counter()
    monkeypatch.setattr(metrics_module, "_dlq_parked", counter)
    return counter


def _app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    get_ingress_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    app.state.notifications_settings = get_notifications_settings()
    app.state.decider = None
    subscriptions_module.register_subscriptions(app)
    return app


@pytest.fixture
def without_dead_lettering(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.delenv("RASK_NOTIFICATIONS_DLQ_TOPIC", raising=False)
    yield _app(monkeypatch)
    get_ingress_settings.cache_clear()


@pytest.fixture
def with_dead_lettering(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("RASK_NOTIFICATIONS_DLQ_TOPIC", DLQ_TOPIC)
    app = _app(monkeypatch)
    with TestClient(app) as client:
        yield client
    get_ingress_settings.cache_clear()


def _declared(app_or_client: FastAPI | TestClient) -> list[dict[str, Any]]:
    client = app_or_client if isinstance(app_or_client, TestClient) else TestClient(app_or_client)
    return client.get("/dapr/subscribe").json()


# --- the default: nothing declared, nothing to park ---------------------------------------------


def test_no_dead_letter_topic_is_declared_by_default(without_dead_lettering: FastAPI) -> None:
    """A `deadLetterTopic` with no retry policy behind it parks on the first transient failure, which
    is strictly worse than having neither: the message is gone from the subscription AND the operator
    has to replay it by hand. Off until the resiliency registration lands with it."""
    declared = _declared(without_dead_lettering)
    assert [entry["topic"] for entry in declared] == ["lineage.events.v1", "catalog.control.v1"]
    # BOTH lanes, because the rule is about the subscription's schedule and not about its payload:
    # a governance delivery that parks on the first transient failure is exactly as lost as a run one.
    assert all("deadLetterTopic" not in entry for entry in declared)


# --- configured: the subscription declares it and the floor exists -------------------------------


def test_configuring_the_topic_declares_it_on_the_subscription(with_dead_lettering: TestClient) -> None:
    declared = {entry["topic"]: entry for entry in _declared(with_dead_lettering)}
    assert declared["lineage.events.v1"]["deadLetterTopic"] == DLQ_TOPIC


def test_a_parked_delivery_is_counted_and_acked(with_dead_lettering: TestClient, parked: _Counter) -> None:
    """Counted BEFORE the log, so a permanently stalled notification is a series something can alert
    on rather than only ERROR scrollback nobody is watching at 03:00."""
    response = with_dead_lettering.post("/dlq-event", json={"id": "ce-9", "topic": "lineage.events.v1", "data": RUN_EVENT})

    assert response.json() == {"status": "SUCCESS"}
    assert parked.adds == [(1, {"lance.notifications.app": "notifications"})]


@pytest.mark.parametrize(
    "envelope",
    [
        {"id": "ce-9", "data": "a string"},
        {"id": "ce-9", "data": {"run": {}}},
    ],
    ids=["string-data", "run-without-an-id"],
)
def test_a_malformed_parked_payload_is_still_parked(envelope: dict[str, Any], with_dead_lettering: TestClient, parked: _Counter) -> None:
    """A parked payload is BY DEFINITION one something already failed to handle, so it may well be the
    reason it failed. Reading it defensively is not politeness here — a raise inside this handler is a
    500 to the sidecar, which is a RETRY, onto the end of the line."""
    assert with_dead_lettering.post("/dlq-event", json=envelope).json() == {"status": "SUCCESS"}
    assert len(parked.adds) == 1


# --- the control lane's second path (LH-331) -----------------------------------------------------

STATE = "http://localhost:3500/v1.0/state/lance-statestore"
GRANT: dict[str, Any] = {
    "event_id": "evt-grant-7",
    "occurred_at": "2026-10-27T12:00:00+00:00",
    "action": "grant_added",
    "object_type": "grant",
    "object_id": "project:acme",
    "actor": "user:dex~admin",
    "extra": {"relation": "member", "subject": "user:dex~alice"},
}


class _StateStore:
    """The sidecar's plain state API as the live daprd with `state.postgresql` answered it (measured 2026-10-27): 204 for
    an absent key, an ETag on every read, a first-write save answering 409 on a stale ETag and 500 "no item was updated"
    when it carries no ETag for a key that exists, and DELETE."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.etags: dict[str, int] = {}
        #: Set while the store is down: every call answers 500.
        self.down = False
        #: How many index writes still find the index moved by another writer between their read and their write.
        self.races = 0

    def get(self, request: httpx.Request, key: str) -> httpx.Response:
        if self.down:
            return httpx.Response(500)
        if key not in self.values:
            return httpx.Response(204)
        return httpx.Response(200, json=self.values[key], headers={"etag": str(self.etags[key])})

    def post(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(500)
        (entry,) = json.loads(request.content)
        key = entry["key"]
        if self.races and key == INDEX_KEY:
            self.races -= 1
            self.etags[key] = self.etags.get(key, 0) + 1
        if "etag" in entry and entry["etag"] != str(self.etags.get(key)):
            return httpx.Response(409)
        if "etag" not in entry and key in self.values and entry.get("options", {}).get("concurrency") == "first-write":
            return httpx.Response(500, json={"errorCode": "ERR_STATE_SAVE", "message": "no item was updated"})
        self.values[key], self.etags[key] = entry["value"], self.etags.get(key, 0) + 1
        return httpx.Response(204)

    def delete(self, request: httpx.Request, key: str) -> httpx.Response:
        self.values.pop(key, None)
        return httpx.Response(204)

    def parked(self) -> list[str]:
        return list(self.values.get(INDEX_KEY, {}).get("event_ids", []))

    def kept_events(self) -> list[str]:
        return sorted(key for key in self.values if key != INDEX_KEY)


class _Inbox:
    def __init__(self, plane: "_Plane", subject: str) -> None:
        self._plane, self._subject = plane, subject

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._plane.down:
            raise RuntimeError("the actor plane is unreachable")
        self._plane.boxes.setdefault(self._subject, []).append(payload["notification_id"])
        return {"delivered": True}

    async def get_watches(self) -> dict[str, Any]:
        return {"projects": [], "total": 0}

    async def erase(self) -> dict[str, Any]:
        self._plane.boxes.pop(self._subject, None)
        return {"removed": []}


class _Plane:
    def __init__(self) -> None:
        self.boxes: dict[str, list[str]] = {}
        self.down = True

    def open(self, subject: str) -> Any:
        return _Inbox(self, subject)


class _QuietFeed:
    """Lineage's feed with nothing past the cursor, so a tick's walk is a no-op beside the re-drive."""

    async def page(self, *, since: int | None) -> FeedPage:
        return FeedPage()


class _Cursor:
    async def get(self) -> LineageCursor:
        return LineageCursor(seq=1, updated_at=datetime.now(UTC))

    async def set(self, seq: int, *, stalls: int = 0) -> None:
        return None


class _ParkPlane:
    """The dead-letter and cron routes on one app, the real park store against the stand-in state API, and an inbox
    plane whose `down` the test controls."""

    def __init__(self, door: TestClient, state: _StateStore, plane: _Plane) -> None:
        self.door, self.state, self.plane = door, state, plane

    def dead_letter(self, data: dict[str, Any]) -> dict[str, str]:
        return self.door.post("/dlq-event", json={"id": f"ce-{data['event_id']}", "topic": "catalog.control.v1", "data": data}).json()

    def tick(self) -> None:
        assert self.door.post(f"/{get_ingress_settings().binding_name}").status_code == 200


@pytest.fixture
def park_plane(with_dead_lettering: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[_ParkPlane]:
    state, plane = _StateStore(), _Plane()
    for module in (reconcile_cron, subscriptions_module):
        monkeypatch.setattr(module, "inbox_for", plane.open)
        monkeypatch.setattr(module, "watch_index_for", plane.open)
    app = cast(FastAPI, with_dead_lettering.app)
    app.include_router(reconcile_cron.router)
    app.state.actors_registered = True
    app.state.lineage_feed = cast(LineageFeedClient, _QuietFeed())
    app.state.lineage_cursor = cast(LineageCursorStore, _Cursor())
    client = httpx.AsyncClient()
    app.state.control_parks = ControlParkStore(client=client, store_name="lance-statestore")
    with respx.mock:
        respx.get(url__regex=rf"{STATE}/(?P<key>[^/]+)$").mock(side_effect=state.get)
        respx.delete(url__regex=rf"{STATE}/(?P<key>[^/]+)$").mock(side_effect=state.delete)
        respx.post(STATE).mock(side_effect=state.post)
        yield _ParkPlane(with_dead_lettering, state, plane)
    asyncio.run(client.aclose())


def test_a_dead_lettered_grant_is_kept_and_reaches_its_person_on_a_later_tick(park_plane: _ParkPlane, parked: _Counter) -> None:
    """LH-331, a guarantee: a governance notification that exhausted its retries is no longer acked into nothing. The
    dead-letter route keeps it, every reconcile tick offers it again through the control ingest, it stays kept while the
    inbox is still down and is retired once it lands, and a park older than the dead-letter stream's retention is
    retired undelivered (counted as `expired`). A dead letter the store cannot keep is retried rather than acked and
    lost, a redelivered one is kept once, and a park whose index write loses a race to another writer reads again and
    lands. An index entry whose event is gone (a retire cut short) is cleared on the next tick. The real routes and the
    real park store run against the sidecar's state API."""
    state, plane = park_plane.state, park_plane.plane
    expired = {**GRANT, "event_id": "evt-too-old", "extra": {"relation": "member", "subject": "user:dex~bob"}}
    too_old = (datetime.now(UTC) - PARK_HORIZON - timedelta(hours=1)).isoformat()
    state.values = {
        park_key("evt-too-old"): {"event_id": "evt-too-old", "event": expired, "parked_at": too_old},
        INDEX_KEY: {"event_ids": ["evt-orphan", "evt-too-old"]},
    }
    state.etags = dict.fromkeys(state.values, 1)

    state.down = True
    assert park_plane.dead_letter(GRANT) == {"status": "RETRY"}
    state.down, state.races = False, 1
    assert park_plane.dead_letter(GRANT) == {"status": "SUCCESS"}
    assert state.parked() == ["evt-orphan", "evt-too-old", "evt-grant-7"]
    assert park_plane.dead_letter(GRANT) == {"status": "SUCCESS"}
    assert (state.parked(), len(parked.adds)) == (["evt-orphan", "evt-too-old", "evt-grant-7"], 1)

    park_plane.tick()
    assert (state.parked(), plane.boxes) == (["evt-grant-7"], {})

    plane.down = False
    park_plane.tick()
    assert (state.parked(), state.kept_events(), plane.boxes) == ([], [], {"dex~alice": ["evt-grant-7@GRANT_ADDED"]})


def test_an_offboarded_persons_parked_notification_is_forgotten_with_their_inbox(park_plane: _ParkPlane) -> None:
    """LH-331, measured live on the dev estate (rev 13): a park is re-driven after the bus has moved on, so a grant
    parked before its person was offboarded landed in the inbox the offboarding had just erased. The erasure now forgets
    every park naming the person, through the real control route, and the next tick tells them nothing; a park naming
    someone else stays."""
    state, plane = park_plane.state, park_plane.plane
    bobs = {**GRANT, "event_id": "evt-for-bob", "extra": {"relation": "member", "subject": "user:dex~bob"}}
    assert park_plane.dead_letter(GRANT) == {"status": "SUCCESS"}
    assert park_plane.dead_letter(bobs) == {"status": "SUCCESS"}
    offboarding = {
        **GRANT,
        "event_id": "evt-offboard",
        "action": "principal_deleted",
        "object_type": "principal",
        "object_id": "user:dex~alice",
        "extra": {"subject": "user:dex~alice"},
    }
    plane.down = False

    erased = park_plane.door.post("/control-events", json={"id": "ce-off", "topic": "catalog.control.v1", "data": offboarding})

    assert erased.json() == {"status": "SUCCESS"}
    assert (state.parked(), state.kept_events()) == (["evt-for-bob"], [park_key("evt-for-bob")])
    park_plane.tick()
    assert plane.boxes == {"dex~bob": ["evt-for-bob@GRANT_ADDED"]}
