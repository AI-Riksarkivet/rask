"""A refusal on the bus route must not be shaped like an ack.

`test_ingress_delivery_door.py` establishes WHO the door refuses. This is the narrower, orthogonal
claim about WHAT a refusal looks like on the wire, and it exists because this route's ordinary
vocabulary makes the mistake easy: `SUCCESS`, `RETRY` and `DROP` are all HTTP 200s, and `DROP` is an
**ack** — the sidecar considers the message handled and never redelivers it.

So a door that answered an unauthenticated delivery with `{"status": "DROP"}` would be indistinguishable
from one that handled a malformed event. Same status code, same body shape, same ingress counter. The
`record_ingress(...DROPPED)` line would even make the graph look right. Nothing would ever surface it.

Two properties, therefore, asserted on the BODY rather than on the status alone: a refusal carries a
reason a caller can act on, and it carries no `status` field for a log pipeline to mistake for one.
"""

import logging
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications.api import subscriptions as subscriptions_module
from notifications.api.settings import get_ingress_settings
from notifications.config import get_notifications_settings
from notifications.proxies import TypedActorProxy
from service_kit.lakehouse.ns_errors import install_problem_handlers


TOKEN = "the-app-api-token"
RUN_EVENT: dict[str, Any] = {
    "eventType": "FAIL",
    "eventTime": "2026-08-09T12:00:00+00:00",
    "run": {"runId": "run-77", "facets": {"author": {"name": "alice", "sub": "alice"}, "lance": {"run_id": "ingest-77"}}},
    "outputs": [{"namespace": "bronze", "name": "bronze$pages"}],
}


class _Inbox:
    def __init__(self, plane: "_Plane", subject: str) -> None:
        self._plane = plane
        self._subject = subject

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = self._plane.boxes.setdefault(self._subject, [])
        rows.append(payload)
        return {"delivered": True, "unread": len(rows), "rows": len(rows)}


class _Plane:
    def __init__(self) -> None:
        self.boxes: dict[str, list[dict[str, Any]]] = {}

    def open(self, subject: str) -> TypedActorProxy:
        return cast(TypedActorProxy, _Inbox(self, subject))


def _cloud_event(event: dict[str, Any]) -> dict[str, Any]:
    return {"id": "ce-1", "source": "lineage", "type": "com.dapr.event.sent", "topic": "lineage.events.v1", "data": event}


@pytest.fixture
def plane() -> _Plane:
    return _Plane()


@pytest.fixture
def door(monkeypatch: pytest.MonkeyPatch, plane: _Plane) -> Iterator[TestClient]:
    monkeypatch.setenv("APP_API_TOKEN", TOKEN)
    get_ingress_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    app.state.notifications_settings = get_notifications_settings()
    app.state.fga = None
    subscriptions_module.register_subscriptions(app)
    # Before any request: the real `inbox_for` builds a Dapr ActorProxy, which blocks on a sidecar
    # health check when there is no daprd to answer it.
    monkeypatch.setattr(subscriptions_module, "inbox_for", plane.open)
    with TestClient(app) as client:
        yield client
    get_ingress_settings.cache_clear()


@pytest.mark.parametrize(
    ("label", "headers"),
    [
        ("a public front door", {"dapr-api-token": TOKEN, "dapr-caller-app-id": "gateway"}),
        ("a wrong credential", {"dapr-api-token": "a-guess"}),
        ("no credential at all", {}),
    ],
)
def test_every_refusal_carries_a_reason_rather_than_only_a_status(door: TestClient, label: str, headers: dict[str, str]) -> None:
    """The reason is what an operator reads when a delivery stops arriving.

    A bare 403 sends whoever is debugging it to the wrong layer: "the gateway is refusing us" and "our
    token secret rendered empty" are the same symptom and different fixes, and only the body separates
    them.
    """
    body = door.post("/lineage-events", json=_cloud_event(RUN_EVENT), headers=headers).json()

    assert body.get("detail"), f"{label} was refused with no reason: {body!r}"


def test_a_refused_delivery_and_a_handled_one_cannot_be_confused_for_each_other(door: TestClient, plane: _Plane) -> None:
    """The contrast IS the assertion.

    Both halves are driven in one test because either alone permits the failure: a refusal asserted
    only as 403 is satisfied by a door that also 403s valid deliveries, and an ack asserted only as
    `SUCCESS` is satisfied by a door that acks everything. What must hold is that the two answers are
    different in shape — status code AND body key — so no log pipeline, metric or eyeball can read one
    as the other.
    """
    handled = door.post("/lineage-events", json=_cloud_event(RUN_EVENT), headers={"dapr-api-token": TOKEN})
    refused = door.post("/lineage-events", json=_cloud_event(RUN_EVENT), headers={"dapr-api-token": TOKEN, "dapr-caller-app-id": "gateway"})

    assert (handled.status_code, handled.json()) == (200, {"status": "SUCCESS"})
    assert refused.status_code == 403
    # THE PROPERTY, RESTATED PRECISELY — and it is stronger than the original, not weaker.
    #
    # This asserted `"status" not in body`. The refusal is now RFC 9457 problem+json
    # (open_fastapi-audit: the guard raised a bare `HTTPException` that no app mapped, so this one 403
    # arrived as `{"detail"}` while every other error in the service carried type/title/code), and
    # problem+json REQUIRES a numeric `status`. The literal assertion and the envelope cannot both hold.
    #
    # What this test actually protects is that a refusal cannot be read as an ACK. Dapr's vocabulary is
    # three STRING verbs — SUCCESS, RETRY, DROP — and DROP is an ack. A numeric 403 is in none of them,
    # and the response is a 403 rather than the 200 that made DROP ambiguous in the first place. So the
    # check is on the VERB, which is what a log pipeline could actually mistake, not on the key name.
    body = refused.json()
    assert body.get("status") not in {"SUCCESS", "RETRY", "DROP"}, (
        "the refusal answers in the sidecar's own vocabulary — DROP is an ACK, and this would be read as one"
    )
    assert body["status"] == 403, "problem+json's `status` must be the numeric code, not a verb"
    assert len(plane.boxes["alice"]) == 1, "exactly the handled delivery landed"


def test_a_malformed_payload_is_the_one_thing_that_looks_like_an_ack_because_it_is_one(door: TestClient, plane: _Plane) -> None:
    """The control for the test above: `DROP` really is a 200 with a `status`.

    Without this the contrast could be satisfied by a door where nothing at all answers `DROP`, and the
    reason a refusal must avoid that shape would be unproven. A permanent parse failure is genuinely
    handled — redelivery cannot fix it — so it acks, and that is exactly why an authentication failure
    must not.
    """
    dropped = door.post("/lineage-events", json={"id": "ce-2"}, headers={"dapr-api-token": TOKEN})

    assert (dropped.status_code, dropped.json()) == (200, {"status": "DROP"})
    assert plane.boxes == {}
