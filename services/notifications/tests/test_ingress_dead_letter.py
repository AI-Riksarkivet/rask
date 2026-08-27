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

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications.api import metrics as metrics_module
from notifications.api import subscriptions as subscriptions_module
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
    app.state.fga = None
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


def test_no_parking_route_exists_when_nothing_can_be_parked(without_dead_lettering: FastAPI) -> None:
    """The route is mounted with the topic, not ahead of it: an unreachable POST endpoint on a public
    port is a surface with no purpose."""
    assert "/dlq-event" not in {getattr(route, "path", "") for route in without_dead_lettering.routes}


# --- configured: the subscription declares it and the floor exists -------------------------------


def test_configuring_the_topic_declares_it_on_the_subscription(with_dead_lettering: TestClient) -> None:
    declared = {entry["topic"]: entry for entry in _declared(with_dead_lettering)}
    assert declared["lineage.events.v1"]["deadLetterTopic"] == DLQ_TOPIC


def test_the_park_route_subscribes_this_apps_own_dead_letter_subject(with_dead_lettering: TestClient) -> None:
    """Its OWN subject, `dlq.notifications`. It already matches the `dlq.>` binding on the existing
    DLQ stream, so per-app naming costs no new NATS stream anywhere — and it is what keeps two
    subscribers of one topic from parking into each other's queue."""
    declared = {entry["topic"]: entry for entry in _declared(with_dead_lettering)}
    assert declared[DLQ_TOPIC]["route"] == "/dlq-event"
    assert declared[DLQ_TOPIC]["pubsubname"] == "lineage-pubsub-notifications"


def test_a_parked_delivery_is_counted_and_acked(with_dead_lettering: TestClient, parked: _Counter) -> None:
    """Counted BEFORE the log, so a permanently stalled notification is a series something can alert
    on rather than only ERROR scrollback nobody is watching at 03:00."""
    response = with_dead_lettering.post("/dlq-event", json={"id": "ce-9", "topic": "lineage.events.v1", "data": RUN_EVENT})

    assert response.json() == {"status": "SUCCESS"}
    assert parked.adds == [(1, {"lance.notifications.app": "notifications"})]


def test_the_park_counter_names_this_app_and_not_the_topic_that_failed(with_dead_lettering: TestClient, parked: _Counter) -> None:
    """`dlq.notifications` → `notifications`. Labelling by the SOURCE topic is the shape that made two
    movers' parks indistinguishable in the cascade's counter — every app subscribed to that topic
    contributes to one series, so the number answers no operational question at all."""
    with_dead_lettering.post("/dlq-event", json={"id": "ce-9", "topic": "lineage.events.v1", "data": RUN_EVENT})

    label = parked.adds[0][1]["lance.notifications.app"]
    assert label == "notifications"
    assert label != "lineage.events.v1"


def test_a_park_is_logged_with_the_run_it_was_about(with_dead_lettering: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """The counter says one delivery was parked; only the log says which run, which is the single
    field that lets an operator find the cause before replaying it."""
    with caplog.at_level(logging.ERROR, logger="notifications.api.dlq"):
        with_dead_lettering.post("/dlq-event", json={"id": "ce-9", "topic": "lineage.events.v1", "data": RUN_EVENT})

    records = [record for record in caplog.records if record.message == "dapr_dead_letter_parked"]
    assert len(records) == 1
    assert tuple(getattr(records[0], field, None) for field in ("app", "dlq_topic", "event_id", "source_topic", "run_id")) == (
        "notifications",
        DLQ_TOPIC,
        "ce-9",
        "lineage.events.v1",
        "run-77",
    )


@pytest.mark.parametrize(
    "envelope",
    [
        {},
        {"id": "ce-9"},
        {"id": "ce-9", "data": None},
        {"id": "ce-9", "data": "a string"},
        {"id": "ce-9", "data": []},
        {"id": "ce-9", "data": {"run": "not-an-object"}},
        {"id": "ce-9", "data": {"run": {}}},
    ],
    ids=["empty", "no-data", "null-data", "string-data", "list-data", "run-not-an-object", "run-without-an-id"],
)
def test_a_malformed_parked_payload_is_still_parked(envelope: dict[str, Any], with_dead_lettering: TestClient, parked: _Counter) -> None:
    """A parked payload is BY DEFINITION one something already failed to handle, so it may well be the
    reason it failed. Reading it defensively is not politeness here — a raise inside this handler is a
    500 to the sidecar, which is a RETRY, onto the end of the line."""
    assert with_dead_lettering.post("/dlq-event", json=envelope).json() == {"status": "SUCCESS"}
    assert len(parked.adds) == 1


def test_a_dead_letter_is_never_asked_to_be_redelivered(with_dead_lettering: TestClient) -> None:
    """The one answer this route may not give. RETRY re-queues a message that has already exhausted a
    full retry schedule, onto a topic with nothing beneath it — an unbounded loop with no backstop,
    and no auto-replay by design: the operator replays from the retained stream once the cause is
    fixed."""
    for envelope in ({"id": "a", "data": RUN_EVENT}, {"id": "b"}, {"id": "c", "data": "junk"}):
        assert with_dead_lettering.post("/dlq-event", json=envelope).json() != {"status": "RETRY"}


def test_a_public_front_door_may_not_park_a_delivery(with_dead_lettering: TestClient, parked: _Counter) -> None:
    """The park route carries the same door as the subscription it backs. It counts and alerts, so an
    unguarded one is a way to make the estate's dead-letter alert fire from outside it."""
    response = with_dead_lettering.post("/dlq-event", headers={"dapr-caller-app-id": "gateway"}, json={"id": "ce-9", "data": RUN_EVENT})

    assert response.status_code == 403
    assert parked.adds == []
