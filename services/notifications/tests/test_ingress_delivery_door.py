"""The bus routes' door: who may put a CloudEvent into somebody's inbox.

The subscriptions are POST routes on the same FastAPI app that serves the public API, so without a
check any client that can reach the port can forge an event — and this plane's forged event is not
a graph row, it is a notification in a named person's inbox, attributed to them as the author.

Three refusals, and they answer different questions:

* **the app token** proves the request arrived through THIS app's sidecar. Unset is the documented dev
  default, and `assert_app_token_configured` turns it into a startup failure the moment Dapr ingest is
  actually on, so the no-op can only apply where nothing is deployed.
* **the caller app-id** refuses a PUBLIC front door outright, token or no token. That one is a
  measured bypass rather than a hypothesis: daprd stamps a valid `dapr-api-token` on every request it
  delivers, and the gateway forwards `/api/*` through Dapr service invocation — so an anonymous
  browser request arrives at a backend already holding a valid service token. It was measured against
  the ingest door as 403 direct, 403 via service DNS, **202 through the gateway**.
* **the signature** ([[XC-078]]) proves who WROTE the event, which neither check above can: both
  authenticate the sidecar that delivered it, and every pod the bus lets publish reaches that sidecar.
  Under `RASK_SIGNATURE_DOORS=enforce` a run event reaches an inbox only when a signer the estate lists
  signed it, and a control event only when the role its action names signed it; the annotator's task
  actions and the grants on its own projects need no signature (owner rulings R3 and R5). A refusal is
  acknowledged and counted, keys the sidecar cannot serve are retried, and `observe` delivers what
  `enforce` would refuse and counts it. Every door and reason series of the refusal counter exists at 0 from
  the moment the doors are registered, so the alert's `rate()` sees the first refusal as an increase. The
  public keys are read through the sidecar's secret API, stood in for by respx; the signatures are built by
  the root conftest's `EventSigner` from the wire format alone.
"""

import importlib
import json
import logging
from collections.abc import Callable, Iterator, Sequence
from typing import Any, cast, get_args

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lineage_kit.signing import RefusalReason
from notifications.api import metrics as metrics_module
from notifications.api import subscriptions as subscriptions_module
from notifications.api.metrics import Door
from notifications.api.settings import get_ingress_settings
from notifications.config import get_notifications_settings
from notifications.proxies import TypedActorProxy
from service_kit.lakehouse.ns_errors import install_problem_handlers


TOKEN = "the-sidecars-app-token"

#: The sidecar's secret API for the store every signer publishes `signing-public-<identity>` in.
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"
CATALOG = "service-catalog"
PRODUCER = "service-medallion-producer"

#: What the signature counters hold: metric name -> (door, reason) -> count.
type Counts = dict[str, dict[tuple[str, str], int]]

REFUSED = "notifications.signature.refused"

#: Every series of the refusal counter, at the 0 it is created with: both doors, every reason the kit can refuse for.
ZERO_REFUSALS: dict[tuple[str, str], int] = {(door.value, reason): 0 for door in Door for reason in get_args(RefusalReason.__value__)}

RUN_EVENT: dict[str, Any] = {
    "eventType": "FAIL",
    "eventTime": "2026-08-09T12:00:00+00:00",
    "run": {"runId": "run-77", "facets": {"author": {"name": "alice", "sub": "alice"}}},
    "outputs": [{"namespace": "bronze", "name": "bronze$pages"}],
}

#: The control envelopes as their producers publish them: the catalog's grant on a table for the person it
#: authenticated, the medallion producer's review request (a service acting for itself), and the annotator's
#: task assignment and grant on one of its own projects.
GRANT: dict[str, Any] = {
    "event_id": "evt-grant",
    "occurred_at": "2026-10-04T12:00:00+00:00",
    "action": "grant_added",
    "object_type": "grant",
    "object_id": "table:acme$gold",
    "actor": "user:admin",
    "extra": {"relation": "reader", "subject": "user:alice"},
}
REVIEW: dict[str, Any] = {
    "event_id": "promotion-review-tok-1",
    "occurred_at": "2026-10-04T12:00:00+00:00",
    "action": "promotion_review_requested",
    "object_type": "table",
    "object_id": "table:acme-gold$pages",
    "actor": None,
    "extra": {"subject": "user:vera", "reasons": ["row count fell by half"], "project": "acme", "token": "tok-1"},
}
TASK: dict[str, Any] = {
    "event_id": "evt-task",
    "occurred_at": "2026-10-04T12:00:00+00:00",
    "action": "task_assigned",
    "object_type": "annotation_task",
    "object_id": "annotation_task:proj-1/task-1",
    "actor": "user:manager",
    "extra": {"subject": "user:bob"},
}
ANNOTATION_GRANT: dict[str, Any] = {
    **GRANT,
    "event_id": "evt-annotation-grant",
    "object_id": "annotation_project:proj-1",
    "actor": "user:manager",
    "extra": {"relation": "annotator", "subject": "user:carol"},
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


def _cloud_event(event: dict[str, Any], topic: str = "lineage.events.v1") -> dict[str, Any]:
    return {"id": "ce-1", "source": "lineage", "type": "com.dapr.event.sent", "topic": topic, "data": event}


def _run_event(case: str, *, catalog: Any, intruder: Any) -> dict[str, Any]:
    """The run event a case delivers. Alice is a person, so only the catalog signs for her, as a delegator."""
    match case:
        case "unsigned":
            return RUN_EVENT
        case "wrong-signer":
            return intruder.sign(RUN_EVENT)
        case "tampered":
            signed = catalog.sign(RUN_EVENT, on_behalf_of="alice")
            signed["outputs"] = [{"namespace": "gold", "name": "gold$pages"}]
            return signed
        case "valid" | "store-down":
            return catalog.sign(RUN_EVENT, on_behalf_of="alice")
        case "unsigned-start":
            return {**RUN_EVENT, "eventType": "START"}
    raise AssertionError(f"no run event for case {case!r}")


def _control_event(case: str, *, catalog: Any, producer: Any) -> dict[str, Any]:
    """The control envelope a case delivers. A grant's actor is a person, so the catalog signs it as a delegator; a
    review request names no actor, so its signer signs it for itself, and only the producer's role may."""
    match case:
        case "unsigned-catalog-grant":
            return GRANT
        case "review-signed-by-another-role":
            return catalog.sign_control(REVIEW)
        case "tampered-grant":
            signed = catalog.sign_control(GRANT, on_behalf_of="user:admin")
            signed["extra"]["subject"] = "user:mallory"
            return signed
        case "catalog-grant":
            return catalog.sign_control(GRANT, on_behalf_of="user:admin")
        case "producer-review-request":
            return producer.sign_control(REVIEW)
        case "unsigned-task":
            return TASK
        case "unsigned-annotation-grant":
            return ANNOTATION_GRANT
        case "unsigned-unnamed-action":
            return {**GRANT, "action": "warehouse_bound", "object_type": "warehouse", "object_id": "warehouse:acme", "extra": {"namespace": "acme"}}
    raise AssertionError(f"no control event for case {case!r}")


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


@pytest.fixture
def signing_door(
    request: pytest.FixtureRequest, plane: _Plane, monkeypatch: pytest.MonkeyPatch, signature_counts: Callable[[], Counts]
) -> Iterator[TestClient]:
    """The token door with signature doors in the mode the case names, and the signer sets the chart renders for this app.

    Built after `signature_counts`, as a pod registers its doors after its MeterProvider is installed: the series the
    registration creates are recorded where the test reads them."""
    for key, value in {
        "APP_API_TOKEN": TOKEN,
        "DAPR_HTTP_PORT": "3500",
        "RASK_SIGNATURE_DOORS": request.param,
        "RASK_EVENT_SIGNERS": json.dumps([CATALOG, PRODUCER]),
        "RASK_EVENT_DELEGATORS": json.dumps([CATALOG]),
        "RASK_CONTROL_SIGNER_ROLES": json.dumps({"catalog": [CATALOG], "medallion_producer": [PRODUCER]}),
    }.items():
        monkeypatch.setenv(key, value)
    yield from _build(plane, monkeypatch)


@pytest.fixture
def signature_counts() -> Iterator[Callable[[], Counts]]:
    """What the signature counters hold, read through a real in-memory reader.

    The counters are created at import against the global meter, so the metrics module is reloaded under a meter
    this test reads, and reloaded again afterwards so every later test records against the global one.
    """
    import opentelemetry.metrics as otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])

    def counts() -> Counts:
        found: Counts = {}
        data = reader.get_metrics_data()
        for resource in data.resource_metrics if data is not None else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if not metric.name.startswith("notifications.signature."):
                        continue
                    # `cast`: the reader types every point as the union of number and histogram shapes, and a counter
                    # yields `NumberDataPoint`, the one carrying a `value`.
                    for point in cast(Sequence[NumberDataPoint], metric.data.data_points):
                        attributes = point.attributes or {}
                        key = (str(attributes.get("lance.notifications.door")), str(attributes.get("lance.notifications.reason")))
                        found.setdefault(metric.name, {})[key] = int(point.value)
        return found

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(otel_metrics, "get_meter", lambda *_args, **_kwargs: provider.get_meter("lance.notifications"))
        importlib.reload(metrics_module)
        yield counts
    importlib.reload(metrics_module)


def _after_one_delivery(door: str, counted: tuple[str, str] | None) -> Counts:
    """The counters after one delivery at ``door``: every refusal series at its 0, plus the one this delivery counted."""
    expected: Counts = {REFUSED: dict(ZERO_REFUSALS)}
    if counted is not None:
        expected.setdefault(counted[0], {})[(door, counted[1])] = 1
    return expected


@respx.mock
@pytest.mark.parametrize(
    ("signing_door", "case", "answer", "boxes", "counted"),
    [
        pytest.param("enforce", "unsigned", "SUCCESS", {}, ("notifications.signature.refused", "unsigned"), id="enforce-an-unsigned-run"),
        pytest.param("enforce", "wrong-signer", "SUCCESS", {}, ("notifications.signature.refused", "signer"), id="enforce-a-signer-the-estate-does-not-list"),
        pytest.param("enforce", "tampered", "SUCCESS", {}, ("notifications.signature.refused", "signature"), id="enforce-a-run-changed-after-it-was-signed"),
        pytest.param("enforce", "valid", "SUCCESS", {"alice": 1}, None, id="enforce-a-run-the-catalog-signed-for-its-author"),
        pytest.param("enforce", "store-down", "RETRY", {}, None, id="enforce-keys-the-sidecar-cannot-serve"),
        pytest.param("enforce", "unsigned-start", "SUCCESS", {}, None, id="enforce-a-run-the-route-ignores-is-not-checked"),
        pytest.param("observe", "unsigned", "SUCCESS", {"alice": 1}, ("notifications.signature.would_refuse", "unsigned"), id="observe-an-unsigned-run"),
    ],
    indirect=["signing_door"],
)
def test_a_delivery_carrying_the_sidecars_token_reaches_an_inbox_only_when_a_listed_signer_signed_it(
    signing_door: TestClient,
    plane: _Plane,
    signature_counts: Callable[[], Counts],
    event_signer: Any,
    respx_allows_unused_routes: None,
    case: str,
    answer: str,
    boxes: dict[str, int],
    counted: tuple[str, str] | None,
) -> None:
    """A run event's author is a claim the token cannot prove: the signature decides whether anyone is told."""
    catalog = event_signer(CATALOG)
    published = respx.get(f"{SECRETS}/signing-public-{CATALOG}")
    published.mock(return_value=httpx.Response(500) if case == "store-down" else httpx.Response(200, json={"keys": catalog.public}))
    event = _run_event(case, catalog=catalog, intruder=event_signer("service-intruder"))
    assert signature_counts() == {REFUSED: ZERO_REFUSALS}, "registering the doors must create every refusal series at 0, or a first refusal is no increase"

    answered = signing_door.post("/lineage-events", headers={"dapr-api-token": TOKEN}, json=_cloud_event(event))

    assert answered.status_code == 200, answered.text
    assert answered.json() == {"status": answer}
    assert {subject: len(rows) for subject, rows in plane.boxes.items()} == boxes
    assert signature_counts() == _after_one_delivery("lineage-events", counted)


@respx.mock
@pytest.mark.parametrize(
    ("signing_door", "case", "boxes", "counted"),
    [
        pytest.param("enforce", "unsigned-catalog-grant", {}, ("notifications.signature.refused", "unsigned"), id="enforce-an-unsigned-catalog-grant"),
        pytest.param("enforce", "review-signed-by-another-role", {}, ("notifications.signature.refused", "signer"), id="enforce-a-review-the-catalog-signed"),
        pytest.param("enforce", "tampered-grant", {}, ("notifications.signature.refused", "signature"), id="enforce-a-grant-changed-after-it-was-signed"),
        pytest.param("enforce", "catalog-grant", {"alice": 1}, None, id="enforce-a-grant-the-catalog-signed-for-its-actor"),
        pytest.param("enforce", "producer-review-request", {"vera": 1}, None, id="enforce-a-review-request-the-producer-signed"),
        pytest.param("enforce", "unsigned-task", {"bob": 1}, None, id="enforce-an-unsigned-task-is-exempt-r3"),
        pytest.param("enforce", "unsigned-annotation-grant", {"carol": 1}, None, id="enforce-an-unsigned-annotation-project-grant-is-exempt-r5"),
        pytest.param("enforce", "unsigned-unnamed-action", {}, None, id="enforce-an-action-the-route-ignores-is-not-checked"),
        pytest.param("observe", "unsigned-catalog-grant", {"alice": 1}, ("notifications.signature.would_refuse", "unsigned"), id="observe-an-unsigned-grant"),
    ],
    indirect=["signing_door"],
)
def test_a_control_event_reaches_an_inbox_only_when_the_role_its_action_names_signed_it(
    signing_door: TestClient,
    plane: _Plane,
    signature_counts: Callable[[], Counts],
    event_signer: Any,
    respx_allows_unused_routes: None,
    case: str,
    boxes: dict[str, int],
    counted: tuple[str, str] | None,
) -> None:
    """A control event's actor is a claim too, and a valid signature from another service is no authority over the action."""
    catalog, producer = event_signer(CATALOG), event_signer(PRODUCER)
    respx.get(f"{SECRETS}/signing-public-{CATALOG}").mock(return_value=httpx.Response(200, json={"keys": catalog.public}))
    respx.get(f"{SECRETS}/signing-public-{PRODUCER}").mock(return_value=httpx.Response(200, json={"keys": producer.public}))
    event = _control_event(case, catalog=catalog, producer=producer)

    answered = signing_door.post("/control-events", headers={"dapr-api-token": TOKEN}, json=_cloud_event(event, topic="catalog.control.v1"))

    assert answered.status_code == 200, answered.text
    assert answered.json() == {"status": "SUCCESS"}
    assert {subject: len(rows) for subject, rows in plane.boxes.items()} == boxes
    assert signature_counts() == _after_one_delivery("control-events", counted)


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


def test_enabling_dapr_ingest_without_a_token_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed at STARTUP. The alternative — serving an unauthenticated ingest route — looks
    configured from every angle: the subscription registers, `/dapr/subscribe` advertises it, and
    deliveries are handled. Refusing to start is the only answer that cannot be missed.

    THE LIFESPAN, NOT `register_subscriptions`, and the move is not a weakening: routes do not serve
    until the lifespan has run, so a lifespan that raises is a pod that never answers. What it buys is
    that the module IMPORTS without a sidecar — mandatory once the token can come from the Dapr secret
    store, because that check performs a store read and `notifications/__init__.py` registers at module
    scope. An import-time store read makes the module unimportable by the probe gate and by this file.
    """
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    monkeypatch.delenv("RASK_APP_TOKEN_FROM_STORE", raising=False)
    get_ingress_settings.cache_clear()

    # Imported here so the refusal is observed through the same lifespan the pod runs, rather than
    # through a re-implementation of it.
    from notifications.lifespan import make_lifespan
    from service_kit.config import Settings

    # `TestClient` as a context manager RUNS the lifespan, which is what a pod's startup does — so the
    # refusal is observed through the real startup path rather than through an async re-enactment of it.
    with pytest.raises(RuntimeError, match="APP_API_TOKEN must be set"), TestClient(FastAPI(lifespan=make_lifespan(Settings()))):
        pass

    get_ingress_settings.cache_clear()


def test_registering_the_subscriptions_needs_no_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property the move exists for: wiring the routes performs NO store read.

    `notifications/__init__.py` calls `register_subscriptions` at module scope, so anything it does is
    done at import. With `RASK_APP_TOKEN_FROM_STORE` set and no daprd reachable, a store read here
    would raise and the module would be unimportable — which is exactly how this surfaced.
    """
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.setenv("RASK_APP_TOKEN_FROM_STORE", "true")
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    get_ingress_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))

    subscriptions_module.register_subscriptions(app)

    assert "/lineage-events" in {getattr(route, "path", "") for route in app.routes}
    get_ingress_settings.cache_clear()
