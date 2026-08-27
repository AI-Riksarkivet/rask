"""The bus route's door: who may put a CloudEvent into somebody's inbox.

The subscription is a POST route on the same FastAPI app that serves the public API, so without a
check any client that can reach the port can forge a run event — and this plane's forged event is not
a graph row, it is a notification in a named person's inbox, attributed to them as the author.

Two refusals, and they answer different questions:

* **the app token** proves the request arrived through THIS app's sidecar. Unset is the documented dev
  default, and `assert_app_token_configured` turns it into a startup failure the moment Dapr ingest is
  actually on, so the no-op can only apply where nothing is deployed.
* **the caller app-id** refuses a PUBLIC front door outright, token or no token. That one is a
  measured bypass rather than a hypothesis: daprd stamps a valid `dapr-api-token` on every request it
  delivers, and the gateway forwards `/api/*` through Dapr service invocation — so an anonymous
  browser request arrives at a backend already holding a valid service token. It was measured against
  the ingest door as 403 direct, 403 via service DNS, **202 through the gateway**.

The old form is reproduced below and shown to ingest the forged event, because "the door refuses it"
is only interesting next to what happens when it does not.
"""

import logging
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications.api import subscriptions as subscriptions_module
from notifications.api.ingest import ingest_run_event
from notifications.api.metrics import Lane
from notifications.api.settings import get_ingress_settings
from notifications.api.visibility import Visibility
from notifications.config import get_notifications_settings
from notifications.proxies import TypedActorProxy
from service_kit.lakehouse.ns_errors import install_problem_handlers


TOKEN = "the-sidecars-app-token"

RUN_EVENT: dict[str, Any] = {
    "eventType": "FAIL",
    "eventTime": "2026-08-09T12:00:00+00:00",
    "run": {"runId": "run-77", "facets": {"author": {"name": "alice", "sub": "alice"}}},
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


def _build(plane: _Plane, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    get_ingress_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    app.state.notifications_settings = get_notifications_settings()
    app.state.fga = None
    subscriptions_module.register_subscriptions(app)
    # Before any request: the real `inbox_for` builds a Dapr `ActorProxy`, whose constructor blocks on
    # a sidecar health check for a full minute when there is no daprd to answer it.
    monkeypatch.setattr(subscriptions_module, "inbox_for", plane.open)
    with TestClient(app) as client:
        yield client
    get_ingress_settings.cache_clear()


@pytest.fixture
def open_door(plane: _Plane, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The documented dev default: no `APP_API_TOKEN`, so the token check is a no-op."""
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    yield from _build(plane, monkeypatch)


@pytest.fixture
def token_door(plane: _Plane, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A deployment where daprd injected `APP_API_TOKEN` from `dapr.io/app-token-secret`."""
    monkeypatch.setenv("APP_API_TOKEN", TOKEN)
    yield from _build(plane, monkeypatch)


def test_a_delivery_carrying_the_sidecars_token_is_ingested(token_door: TestClient, plane: _Plane) -> None:
    assert token_door.post("/lineage-events", headers={"dapr-api-token": TOKEN}, json=_cloud_event(RUN_EVENT)).json() == {"status": "SUCCESS"}
    assert len(plane.boxes["alice"]) == 1


@pytest.mark.parametrize("headers", [{}, {"dapr-api-token": "a-guess"}, {"dapr-api-token": ""}], ids=["no-token", "wrong-token", "empty-token"])
def test_a_delivery_without_the_sidecars_token_puts_nothing_in_an_inbox(headers: dict[str, str], token_door: TestClient, plane: _Plane) -> None:
    """Boundary by name: absent, wrong, and blank are one refusal. A blank one is the case worth
    naming — a token secret that renders empty would otherwise silently reopen the route while every
    manifest still says it is authenticated."""
    assert token_door.post("/lineage-events", headers=headers, json=_cloud_event(RUN_EVENT)).status_code == 403
    assert plane.boxes == {}


def test_a_public_front_door_may_not_deliver_an_event_even_with_a_valid_token(token_door: TestClient, plane: _Plane) -> None:
    """The token authenticates the PROXY, never the caller behind it. The gateway is a trusted service
    invoking on behalf of someone who is not, so its invocations of a sidecar-delivery route are
    refused unconditionally — this is not a token question and must not be answered like one."""
    response = token_door.post(
        "/lineage-events",
        headers={"dapr-api-token": TOKEN, "dapr-caller-app-id": "gateway"},
        json=_cloud_event(RUN_EVENT),
    )

    assert response.status_code == 403
    assert "public front door" in response.json()["detail"]
    assert plane.boxes == {}


def test_the_public_front_door_is_refused_in_dev_too_where_it_is_the_only_guard(open_door: TestClient, plane: _Plane) -> None:
    """With no token configured the caller check is the whole door. Making it conditional on the token
    would leave the least-configured deployment the most permissive one."""
    assert open_door.post("/lineage-events", headers={"dapr-caller-app-id": "gateway"}, json=_cloud_event(RUN_EVENT)).status_code == 403
    assert plane.boxes == {}


def test_an_absent_caller_header_is_not_a_public_caller(open_door: TestClient, plane: _Plane) -> None:
    """Load-bearing rather than lenient: pub/sub delivery, binding delivery and a direct Service-DNS
    call all arrive with no `dapr-caller-app-id`, and those are exactly the legitimate paths onto this
    route. Treating absence as public would refuse every real delivery while closing nothing."""
    assert open_door.post("/lineage-events", json=_cloud_event(RUN_EVENT)).json() == {"status": "SUCCESS"}
    assert len(plane.boxes["alice"]) == 1


@pytest.mark.asyncio
async def test_a_route_without_the_door_would_put_a_forged_run_in_a_named_persons_inbox(plane: _Plane) -> None:
    """The old form, built and shown broken.

    This is the whole payload: an attacker names the author, and the ingress believes it — because the
    author facet is only trustworthy by virtue of the doors that write it (the HTTP door overwrites it
    with the token sub; the catalog stamps it at emit). Nothing downstream re-derives it, and nothing
    can: a notification attributed to alice is indistinguishable from one alice caused.
    """
    forged = {
        "eventType": "FAIL",
        "eventTime": "2026-08-09T12:00:00+00:00",
        "run": {"runId": "forged-1", "facets": {"author": {"name": "alice", "sub": "alice"}}},
        "outputs": [{"namespace": "bronze", "name": "bronze$pages"}],
    }

    await ingest_run_event(forged, lane=Lane.BUS, visibility=Visibility(client=None, enabled=False), open_inbox=plane.open)

    assert plane.boxes["alice"][0]["notification_id"] == "forged-1@FAIL"


def test_enabling_dapr_ingest_without_a_token_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed at BUILD time. The alternative — starting with an unauthenticated ingest route —
    looks configured from every angle: the subscription registers, `/dapr/subscribe` advertises it, and
    deliveries are handled. Refusing to start is the only answer that cannot be missed."""
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    get_ingress_settings.cache_clear()

    with pytest.raises(RuntimeError, match="APP_API_TOKEN must be set"):
        subscriptions_module.register_subscriptions(FastAPI())

    get_ingress_settings.cache_clear()


def test_dapr_ingest_with_a_token_builds_the_subscription(monkeypatch: pytest.MonkeyPatch, plane: _Plane) -> None:
    """The other side of the guard: a correctly configured deployment is not refused."""
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.setenv("APP_API_TOKEN", TOKEN)
    get_ingress_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))

    subscriptions_module.register_subscriptions(app)

    assert "/lineage-events" in {getattr(route, "path", "") for route in app.routes}
    get_ingress_settings.cache_clear()
