"""Any catalog replica answers the events poll, with the cursor any other replica gave (LH-601, LH-583).

`GET /management/v1/events` reads the control topic's durable stream, and its cursor is the stream's sequence, which is
the same on every replica. So the Service needs no session affinity: a console handed from one replica to another misses
no event and repeats none, and is told to reset only when the stream no longer holds what came after its cursor.

What the poll serves is what a control signer signed ([[CTL-32]], ADR 0171): an event the enforcing door refuses is
passed over, and one whose signers' keys cannot be read yet is not passed over: the poll answers up to it, and serves it
once the keys are read.

Two catalog apps stand for two replicas: they share one bus, whose stream is what the catalog's emitter published on it
(`tests.bus_double.BusDouble`). The keys are read from the secret store, which the suite's double answers.
"""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.api.control_lane import TOPIC
from catalog.api.control_relay import attach_published_keys
from catalog.api.dependencies import request_settings
from catalog.core.config import get_settings
from lakehouse.governed.secrets import SecretStore
from lakehouse.governed.signing_key import signing_public_secret
from tests.bus_double import BusDouble
from tests.catalog_settings import catalog_settings
from tests.secret_store_double import SecretStoreDouble


CATALOG = "kubernetes~system:serviceaccount:default:rask-sa-catalog"


def _event(n: int) -> dict[str, Any]:
    return {
        "event_id": f"event-{n}",
        "occurred_at": "2026-11-16T12:00:00+00:00",
        "action": "namespace_dropped",
        "object_type": "namespace",
        "object_id": f"namespace:{n:08d}-8a3b-4e6f-b1d0-5c9a2e8f3b47",
        "actor": "user:dex~alice",
        "extra": {},
    }


class _Estate:
    """Two catalog replicas on one bus, and the catalog's signer."""

    def __init__(self, bus: BusDouble, replicas: tuple[TestClient, TestClient], signer: Any) -> None:
        self.bus = bus
        self.replicas = replicas
        self.signer = signer

    def sign(self, event: dict[str, Any]) -> dict[str, Any]:
        """``event`` as the catalog signs it, on behalf of the person it names."""
        return self.signer.sign_control(event, on_behalf_of=event["actor"])

    def publish(self, *events: dict[str, Any]) -> None:
        """Put ``events`` on the control topic's stream, each in the envelope the catalog publishes it in."""
        for event in events:
            self.bus.take(TOPIC, {"data": event})

    def poll(self, replica: int, since: int) -> tuple[list[str], int, bool]:
        answer = self.replicas[replica].get("/management/v1/events", params={"since": since})
        assert answer.status_code == 200, answer.text
        body = answer.json()
        return [event["event_id"] for event in body["events"]], body["cursor"], body["reset"]


@pytest.fixture
def estate(
    monkeypatch: pytest.MonkeyPatch, event_signer: Any, secret_store: SecretStoreDouble, secret_session: SecretStore, respx_allows_unused_routes: None
) -> Iterator[_Estate]:
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("RASK_BUS_URL", "nats://bus.test:4222")
    monkeypatch.setenv("RASK_BUS_USER", "catalog")
    get_settings.cache_clear()
    signer = event_signer(CATALOG)
    settings = catalog_settings(
        LANCE_CONTROL_EMIT_ENABLED="true",
        LANCE_CONTROL_PAGE_SIZE=4,
        RASK_SIGNATURE_DOORS="enforce",
        RASK_EVENT_SIGNERS=[CATALOG],
        RASK_EVENT_DELEGATORS=[CATALOG],
        RASK_CONTROL_SIGNER_ROLES={"catalog": [CATALOG]},
    )
    secret_store.hold(signing_public_secret(signer.identity), {"keys": signer.public})
    from catalog.api.v1.endpoints import events

    bus = BusDouble()
    apps = []
    for _ in range(2):
        app = FastAPI()
        app.include_router(events.management_router)
        app.dependency_overrides[request_settings] = lambda: settings
        app.state.bus = bus
        # Authorization off, as the lifespan wires it for a catalog with FGA off.
        app.state.fga = None
        attach_published_keys(app, secret_session)
        apps.append(app)
    try:
        with TestClient(apps[0]) as first, TestClient(apps[1]) as second:
            yield _Estate(bus, (first, second), signer)
    finally:
        get_settings.cache_clear()


def test_a_poller_handed_to_another_replica_misses_nothing_and_resets_only_when_the_stream_moved_on(estate: _Estate) -> None:
    sign = estate.sign
    estate.publish(sign(_event(1)), sign(_event(2)), sign(_event(3)))
    seen, cursor, reset = estate.poll(0, 0)
    estate.publish(sign(_event(4)), sign(_event(5)))

    handed_on = estate.poll(1, cursor)
    estate.bus.expire_through(4)
    after_expiry = estate.poll(0, cursor)

    assert (seen, reset) == (["event-1", "event-2", "event-3"], False)
    assert handed_on == (["event-4", "event-5"], 5, False)
    assert after_expiry == (["event-5"], 5, True)


def test_a_forged_event_is_passed_over_and_one_whose_keys_cannot_be_read_yet_is_served_once_they_are(estate: _Estate, secret_store: SecretStoreDouble) -> None:
    sign = estate.sign
    estate.publish(sign(_event(1)), _event(2), sign(_event(3)))
    keys = secret_store.hold(signing_public_secret(CATALOG), httpx.Response(503))
    keys.side_effect = [httpx.Response(503), SecretStoreDouble.held({"keys": estate.signer.public})]
    held = estate.poll(1, 0)

    served = estate.poll(1, held[1])

    assert held == ([], 0, False)
    assert served == (["event-1", "event-3"], 3, False)


def test_only_a_poll_that_delivers_or_resets_is_audited(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """A console polls every few seconds, so an empty poll writes nothing to the audit trail; one that delivers events, or
    tells the caller to reset, writes one record."""
    from catalog.api.v1.endpoints import events

    audited: list[dict[str, Any]] = []
    monkeypatch.setattr(events, "audit", lambda _action, _outcome, **fields: audited.append(fields))
    estate.publish(estate.sign(_event(1)), estate.sign(_event(2)))

    _, cursor, _ = estate.poll(0, 0)
    estate.poll(1, cursor)
    estate.publish(estate.sign(_event(3)), estate.sign(_event(4)))
    estate.bus.expire_through(3)
    estate.poll(0, cursor)

    assert [(fields["delivered"], fields["reset"], fields["resource"]) for fields in audited] == [(2, False, "estate:rask"), (1, True, "estate:rask")]
