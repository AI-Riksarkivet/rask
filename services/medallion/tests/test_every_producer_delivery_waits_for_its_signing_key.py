"""Every sidecar-delivered route of the medallion producer is decided at its door, before its handler acts on the delivery.

TWO GATES, IN THIS ORDER.

THE PRODUCER'S OWN KEY ([[LH-064]]). Dapr delivers a message to a pod whatever its readiness says, so a producer that is
waiting for its signing key would otherwise start the cascade, hold a promotion and schedule a training watch from events
it could not sign. A DROP would lose the work and a malformed trigger is dropped, so the answer comes before the trigger is
parsed: RETRY on all four routes, each wired separately, so a route added without the gate is a delivery that is lost.

THE EVENT'S SIGNATURE ([[XC-078]]). Each cascade head starts work in a tenant's name on what an event says, and the bus
authenticates the sidecar that delivered the event, never the producer that wrote it. So `/bronze-arrival` acts only on a
bronze write a listed lineage signer signed, and `/publication-arrival` only on a `table_published` the catalog signed.
Enforcing, a refusal is acknowledged, counted and drives nothing (a DROP would park it, and park it again on every replay),
and a key list the store cannot serve is retried; observing, the head acts as before and counts what it would have
refused. An event the head ignores is never verified, so it is counted nowhere. Every refusal series is exported at zero
from the moment the routes are registered, so a first refusal is a rate the page can see.

DECIDING COSTS ONE LISTING AT MOST. Whether the bronze head acts on a write it does not recognise is a question for the
declared lanes, and reading them is one LIST and one GET per stored declaration: the head lists them at most once per
delivery, whatever the event names, and never on the event loop.

Driven through the registered routes with the production wiring: the public keys are read through the sidecar's secret
API, stood in for by respx at the address the sidecar serves; the signatures are built by the root conftest's
`EventSigner` from the wire format alone; the counters are read from a real in-memory metric reader. The producer's own
lifespan resolves its key for readiness the way the stage runner's does, and that wiring is the second test here.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Callable, Iterator, Sequence
from typing import Any, Final, Literal, cast, get_args
from unittest import mock

import httpx
import opentelemetry.metrics as otel_metrics
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from pydantic import BaseModel, ConfigDict, Field

from lineage_kit import RefusalReason, SigningKey, parse_published_keys
from medallion.api.bronze_arrival import register_bronze_arrival_route
from medallion.api.promotions import register_promotion_route
from medallion.api.train import register_train_trigger_route
from medallion.core import metrics
from medallion.core.config import get_settings
from service_kit.governed.signing_key import SigningKeyHolder, attach_signing
from service_kit.lakehouse import transform_specs
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.transform_specs import TransformSpec


PRODUCER: Final = "service-medallion-producer"
CATALOG: Final = "service-catalog"
#: A listed lineage signer whose published key list the store does not serve.
INGEST: Final = "service-ingest"
#: An identity no signer set lists.
UNLISTED: Final = "service-notifications"
#: Who the catalog stamps as the actor of a stage runner's publish: the runner, authenticated as itself.
RUNNER: Final = "user:service-bronze-to-silver"
SECRETS: Final = "http://localhost:3500/v1.0/secrets/lance-secrets"
TOKEN: Final = "the-estate-app-token"
ROLES: Final = {"catalog": [CATALOG], "medallion_producer": [PRODUCER]}

REFUSED: Final = "medallion.signature.refused"
WOULD_REFUSE: Final = "medallion.signature.would_refuse"
#: The refusal series both heads export at zero from the start, one per door and refusal reason: the page reads their rate,
#: and a series born at one by the first refusal has none.
ZERO_REFUSALS: Final = {(REFUSED, door, reason): 0 for door in ("bronze-arrival", "publication-arrival") for reason in get_args(RefusalReason.__value__)}

type Signers = dict[str, Any]


class _Case(BaseModel):
    """One delivery to one route: the producer's key, the door's settings, what arrives, and what the producer does with it."""

    model_config = ConfigDict(frozen=True)

    route: str
    deliver: Callable[[Signers], dict[str, Any]]
    own_key: Literal["absent", "waiting"] = "absent"
    mode: Literal["observe", "enforce"] = "enforce"
    roles: dict[str, list[str]] = Field(default_factory=lambda: dict(ROLES))
    answer: Literal["SUCCESS", "RETRY"] = "SUCCESS"
    #: The topics the head publishes a trigger on: empty when it drives nothing.
    published: list[str] = Field(default_factory=list)
    #: (counter, door, reason) -> value, for every signature counter that moved.
    counted: dict[tuple[str, str, str], int] = Field(default_factory=dict)
    #: How many times the head listed the declared lanes to decide; each listing must leave the event loop free.
    listed: int = 0


def _bronze_write(
    *, author: str = PRODUCER, operation: str = "lance_ray_ingest", tier: str = "bronze", tables: tuple[str, ...] = ("events",)
) -> dict[str, Any]:
    """A batch landed in tenant `acme`'s tier, for alice: the COMPLETE run `/produce` emits for a bronze write."""
    namespace = f"acme-{tier}"
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-10-04T12:00:00+00:00",
        "run": {
            "runId": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
            "facets": {
                "author": {"name": author, "sub": author},
                "lance": {"operation": operation, "project": "acme", "token": "tok-1", "originator": "alice"},
            },
        },
        "job": {"namespace": "medallion", "name": "produce"},
        "outputs": [{"namespace": namespace, "name": f"{namespace}${table}"} for table in tables],
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
    }


def _control_event(action: str, object_type: str, object_id: str, actor: str, extra: dict[str, Any]) -> dict[str, Any]:
    """A control event as the bus carries it: `CatalogControlEvent` in its JSON form."""
    return {
        "event_id": "0b5c3f1e9d2a4c6b8e7f1a2b3c4d5e6f",
        "occurred_at": "2026-10-04T12:00:00Z",
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "actor": actor,
        "extra": extra,
    }


def _table_published() -> dict[str, Any]:
    """A stage runner's publish moved `published` on tenant `acme`'s silver table, for alice's batch."""
    extra = {"project": "acme", "from_version": 3, "to_version": 7, "location": "s3://wh/acme-silver$features", "cascade_id": "tok-1", "originator": "alice"}
    return _control_event("table_published", "table", "table:acme-silver$features", RUNNER, extra)


def _grant_added() -> dict[str, Any]:
    return _control_event("grant_added", "grant", "project:acme", "user:alice", {"relation": "reader", "subject": "user:bob"})


def _after_signing(signed: dict[str, Any], path: tuple[str, ...], value: object) -> dict[str, Any]:
    """``signed`` with one member rewritten after its signer signed it, as a hop that edits the bytes would."""
    holder = signed
    for key in path[:-1]:
        holder = holder[key]
    holder[path[-1]] = value
    return signed


def _waiting(_signers: Signers) -> dict[str, Any]:
    """Not a trigger at all: a handler that read it would drop it, so only an answer given first can retry it."""
    return {"token": ["not", "a", "trigger"]}


CASES = [
    *(
        pytest.param(_Case(route=route, deliver=_waiting, own_key="waiting", answer="RETRY"), id=f"{name}-while-the-producer-waits-for-its-key")
        for route, name in [
            ("/bronze-arrival", "the-bronze-write-that-wakes-the-cascade"),
            ("/publication-arrival", "a-publication-that-wakes-it"),
            ("/train-trigger", "a-training-request"),
            ("/promotion-held", "a-promotion-held-for-review"),
        ]
    ),
    pytest.param(
        _Case(route="/bronze-arrival", deliver=lambda _s: _bronze_write(), counted={(REFUSED, "bronze-arrival", "unsigned"): 1}),
        id="an-unsigned-bronze-write-is-refused",
    ),
    pytest.param(
        _Case(route="/bronze-arrival", deliver=lambda s: s[UNLISTED].sign(_bronze_write()), counted={(REFUSED, "bronze-arrival", "signer"): 1}),
        id="a-bronze-write-no-listed-signer-signed-is-refused",
    ),
    pytest.param(
        _Case(
            route="/bronze-arrival",
            deliver=lambda s: _after_signing(s[PRODUCER].sign(_bronze_write()), ("run", "facets", "lance", "originator"), "mallory"),
            counted={(REFUSED, "bronze-arrival", "signature"): 1},
        ),
        id="a-bronze-write-changed-after-it-was-signed-is-refused",
    ),
    pytest.param(
        _Case(route="/bronze-arrival", deliver=lambda s: s[PRODUCER].sign(_bronze_write()), published=["medallion.bronze"]),
        id="a-bronze-write-signed-by-its-author-wakes-the-cascade",
    ),
    pytest.param(
        _Case(
            route="/bronze-arrival",
            deliver=lambda s: s[CATALOG].sign(_bronze_write(author="alice", operation="append"), on_behalf_of="alice"),
            published=["medallion.bronze"],
        ),
        id="a-persons-bronze-write-the-delegator-signed-for-wakes-the-cascade",
    ),
    pytest.param(
        _Case(route="/bronze-arrival", deliver=lambda s: s[INGEST].sign(_bronze_write(author=INGEST, operation="ingest")), answer="RETRY"),
        id="a-bronze-write-whose-signers-keys-cannot-be-read-is-retried",
    ),
    pytest.param(
        _Case(
            route="/bronze-arrival",
            deliver=lambda _s: _bronze_write(),
            mode="observe",
            published=["medallion.bronze"],
            counted={(WOULD_REFUSE, "bronze-arrival", "unsigned"): 1},
        ),
        id="observing-an-unsigned-bronze-write-still-wakes-the-cascade-and-counts-it",
    ),
    pytest.param(
        _Case(route="/bronze-arrival", deliver=lambda _s: _bronze_write(tier="silver"), listed=1),
        id="an-unsigned-write-the-bronze-head-ignores-is-not-verified",
    ),
    pytest.param(
        _Case(route="/bronze-arrival", deliver=lambda _s: _bronze_write(tier="silver", tables=("events", "features", "pages")), listed=1),
        id="a-write-naming-many-tables-is-decided-on-one-listing-of-the-declared-lanes",
    ),
    # The catalog stamps the tenant on every write it announces, so its compaction of acme's bronze table names exactly the
    # pair the head fires on, and its signature verifies: only the operation says no batch landed.
    pytest.param(
        _Case(
            route="/bronze-arrival",
            deliver=lambda s: s[CATALOG].sign(_bronze_write(author="alice", operation="compact_table"), on_behalf_of="alice"),
        ),
        id="a-maintenance-pass-on-the-bronze-table-starts-no-cascade",
    ),
    pytest.param(
        _Case(route="/publication-arrival", deliver=lambda _s: _table_published(), counted={(REFUSED, "publication-arrival", "unsigned"): 1}),
        id="an-unsigned-publication-is-refused",
    ),
    pytest.param(
        _Case(
            route="/publication-arrival",
            deliver=lambda s: s[PRODUCER].sign_control(_table_published(), on_behalf_of=RUNNER),
            counted={(REFUSED, "publication-arrival", "signer"): 1},
        ),
        id="a-publication-another-service-signed-is-refused",
    ),
    pytest.param(
        _Case(
            route="/publication-arrival",
            deliver=lambda s: _after_signing(s[CATALOG].sign_control(_table_published(), on_behalf_of=RUNNER), ("extra", "originator"), "mallory"),
            counted={(REFUSED, "publication-arrival", "signature"): 1},
        ),
        id="a-publication-changed-after-it-was-signed-is-refused",
    ),
    pytest.param(
        _Case(route="/publication-arrival", deliver=lambda s: s[CATALOG].sign_control(_table_published(), on_behalf_of=RUNNER), published=["medallion.silver"]),
        id="a-publication-the-catalog-signed-wakes-the-next-tier",
    ),
    pytest.param(
        _Case(
            route="/publication-arrival",
            deliver=lambda _s: _table_published(),
            mode="observe",
            published=["medallion.silver"],
            counted={(WOULD_REFUSE, "publication-arrival", "unsigned"): 1},
        ),
        id="observing-an-unsigned-publication-still-wakes-the-next-tier-and-counts-it",
    ),
    pytest.param(
        _Case(route="/publication-arrival", deliver=lambda _s: _grant_added()),
        id="an-unsigned-control-event-the-publication-head-ignores-is-not-verified",
    ),
    pytest.param(
        _Case(
            route="/publication-arrival",
            deliver=lambda s: s[CATALOG].sign_control(_table_published(), on_behalf_of=RUNNER),
            roles={"medallion_producer": [PRODUCER]},
            counted={(REFUSED, "publication-arrival", "signer"): 1},
        ),
        id="a-publication-is-refused-when-the-chart-names-no-catalog-signer",
    ),
]


class _Sidecar:
    """The Dapr client a head publishes its trigger through, with the real `publish_event` signature, recording each topic."""

    def __init__(self) -> None:
        self.topics: list[str] = []

    async def publish_event(
        self,
        pubsub_name: str,
        topic_name: str,
        data: bytes | str,
        publish_metadata: dict[str, str] | None = None,
        metadata: tuple[tuple[str, str | bytes], ...] | None = None,
        data_content_type: str | None = None,
    ) -> None:
        self.topics.append(topic_name)


class _Registry:
    """The control root's transform declarations, read through `transform_specs.list_specs` and its signature: none declared.

    A real listing is one LIST and one GET per stored declaration, so each call is recorded with where it ran: a listing
    taken on the event loop holds every other delivery and the probes behind it.
    """

    def __init__(self) -> None:
        self.listings: list[str] = []

    def list_specs(self, control_root: str, storage_options: StorageOptions, project: str | None = None) -> list[TransformSpec]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.listings.append("in a worker thread")
        else:
            self.listings.append("on the event loop")
        return []


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    """The medallion's instruments bound to a real in-memory reader for one test, and rebound to the process's provider after.

    The instruments are made when `medallion.core.metrics` is imported, so the module is imported again under a meter of
    this reader's provider; every `record_*` reads its instrument from the module, so whatever records reaches the reader.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    with mock.patch.object(otel_metrics, "get_meter", lambda name, *_args, **_kwargs: provider.get_meter(name)):
        importlib.reload(metrics)
    yield reader
    importlib.reload(metrics)
    provider.shutdown()


def _signature_counts(reader: InMemoryMetricReader) -> dict[tuple[str, str, str], int]:
    """Every point of the medallion's signature counters, keyed by (counter, door, reason)."""
    counted: dict[tuple[str, str, str], int] = {}
    data = reader.get_metrics_data()
    for resource in data.resource_metrics if data is not None else ():
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if not metric.name.startswith("medallion.signature."):
                    continue
                # A counter only ever yields `NumberDataPoint`, the member of the reader's point union that has a value.
                for point in cast(Sequence[NumberDataPoint], metric.data.data_points):
                    attributes = point.attributes or {}
                    key = (metric.name, str(attributes.get("lance.medallion.door")), str(attributes.get("lance.medallion.reason")))
                    counted[key] = int(point.value)
    return counted


@pytest.fixture
def producer_settings() -> Iterator[None]:
    """Settings read afresh from the environment this test sets, and forgotten after it."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _producer(monkeypatch: pytest.MonkeyPatch, case: _Case, sidecar: _Sidecar) -> TestClient:
    """The producer's four sidecar-delivered routes as the chart renders them with the control lane and the doors on."""
    for name, value in {
        "APP_API_TOKEN": TOKEN,
        "DAPR_HTTP_PORT": "3500",
        "MEDALLION_CONTROL_ROOT": "s3://lakehouse",
        "MEDALLION_CONTROL_PUBSUB": "catalog-control-pubsub",
        "MEDALLION_TRANSFORM_ROUTES": json.dumps({"silver": "medallion.silver"}),
        "RASK_SIGNATURE_DOORS": case.mode,
        "RASK_EVENT_SIGNERS": json.dumps([PRODUCER, CATALOG, INGEST]),
        "RASK_EVENT_DELEGATORS": json.dumps([CATALOG]),
        "RASK_CONTROL_SIGNER_ROLES": json.dumps(case.roles),
    }.items():
        monkeypatch.setenv(name, value)
    app = FastAPI()
    app.state.dapr = sidecar
    waiting = SigningKeyHolder(identity=PRODUCER, store="lance-secrets", load_key=SigningKey.from_seed, parse_published=parse_published_keys)
    attach_signing(app, waiting if case.own_key == "waiting" else None)
    wrapper = register_bronze_arrival_route(app)
    register_train_trigger_route(app, wrapper)
    register_promotion_route(app, wrapper)
    return TestClient(app)


@respx.mock
@pytest.mark.parametrize("case", CASES)
def test_every_producer_delivery_is_decided_before_its_handler_reads_it(
    monkeypatch: pytest.MonkeyPatch,
    producer_settings: None,
    metric_reader: InMemoryMetricReader,
    event_signer: Any,
    respx_allows_unused_routes: None,
    case: _Case,
) -> None:
    signers: Signers = {identity: event_signer(identity) for identity in (PRODUCER, CATALOG, INGEST, UNLISTED)}
    for identity in (PRODUCER, CATALOG):
        respx.get(f"{SECRETS}/signing-public-{identity}").mock(return_value=httpx.Response(200, json={"keys": signers[identity].public}))
    respx.get(f"{SECRETS}/signing-public-{INGEST}").mock(return_value=httpx.Response(500))
    registry = _Registry()
    monkeypatch.setattr(transform_specs, "list_specs", registry.list_specs)
    sidecar = _Sidecar()
    client = _producer(monkeypatch, case, sidecar)

    answered = client.post(case.route, json={"data": case.deliver(signers)}, headers={"dapr-api-token": TOKEN})

    assert (answered.status_code, answered.json()) == (200, {"status": case.answer})
    assert sidecar.topics == case.published, f"the head published triggers on {sidecar.topics}"
    assert _signature_counts(metric_reader) == {**ZERO_REFUSALS, **case.counted}
    assert registry.listings == ["in a worker thread"] * case.listed


class _SidecarAtShutdown:
    """What the producer does with its Dapr client at shutdown."""

    async def close(self) -> None:
        return None


def test_a_producer_without_its_key_is_not_ready(monkeypatch: pytest.MonkeyPatch, respx_allows_unused_routes: None) -> None:
    identity = "service-medallion-producer"
    secrets = "http://localhost:3500/v1.0/secrets/lance-secrets"
    env = {
        "MEDALLION_SECRETS_FROM_DAPR": "true",
        "RASK_SIGNING_IDENTITY": identity,
        "MEDALLION_FGA_SERVICE_IDENTITY": identity,
        "DAPR_HTTP_PORT": "3500",
        "APP_API_TOKEN": "the-estate-app-token",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    from medallion import producer

    get_settings.cache_clear()
    monkeypatch.setattr(producer, "DaprClient", _SidecarAtShutdown)
    monkeypatch.setattr(producer, "instrument_lance_if_available", lambda: None)
    monkeypatch.setattr(producer, "register_tasks", lambda _settings: None)
    with respx.mock:
        respx.get(f"{secrets}/lance").mock(return_value=httpx.Response(200, json={"minio-secret-key": "the-s3-secret"}))
        respx.get(f"{secrets}/signing-key-{identity}").mock(return_value=httpx.Response(500))
        respx.get(f"{secrets}/signing-public-{identity}").mock(return_value=httpx.Response(404))
        with TestClient(producer.app) as client:
            readiness, liveness = client.get("/readyz"), client.get("/livez")
    get_settings.cache_clear()

    assert (readiness.status_code, liveness.status_code) == (503, 200)
