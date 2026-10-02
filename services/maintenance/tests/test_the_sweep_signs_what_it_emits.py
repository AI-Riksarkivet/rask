"""Maintenance signs every lineage event it emits as itself, authors its failures, and emits nothing without its key.

[[LH-064]]. A verifier refuses an unsigned event, and a signature only verifies when the signer is the event's author,
so the service stamps its own identity as the author of EVERY event it emits and signs as that identity. That includes a
FAIL: it was authorless, which a verifier could never accept, and an unattributed failure is the one a person is most
likely to have to chase.

A SIGNER WITHOUT ITS KEY EMITS NOTHING and reports itself not ready, and every delivery that would make it emit (a unit
of work, an index build, a write arrival, the sweep's own tick) is answered before the delivery is read, so it is
redelivered or retried rather than lost. The sweep emits after the work it describes, so a signature that cannot be
made is a withheld event and never a failed sweep.

The first two claims are driven through the real service lifespan, so the key reaches the emitter the way it does in the
cluster: the secret store, the holder, the emitter and its author. The sidecar's secret API is answered by respx and its
publish is a recording stand-in.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any, ClassVar, cast

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lineage_kit import SigningKey, parse_published_keys, verify_signature
from maintenance.api.arrival import register_arrival_route
from maintenance.api.index_work import register_index_route
from maintenance.api.routes import build_router
from maintenance.api.work import register_work_route
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import DaprMaintenanceEmitter, NoopEmitter
from service_kit.governed.signing_key import SigningKeyHolder, attach_signing


if TYPE_CHECKING:
    from dapr.aio.clients import DaprClient


IDENTITY = "service-maintenance"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"


class _Sidecar:
    """What the service does with its Dapr client: publish, and close at shutdown."""

    published: ClassVar[list[dict[str, Any]]] = []

    async def publish_event(self, *, data: str = "", **_kwargs: object) -> None:
        _Sidecar.published.append(json.loads(data))

    async def close(self) -> None:
        return None


class _Published:
    """One identity's public keys, as lineage would read them."""

    def __init__(self, *keys: str) -> None:
        self._keys = keys

    def published(self, identity: str) -> Sequence[str]:
        return self._keys

    def refresh(self, identity: str) -> Sequence[str]:
        return self._keys


type Boot = Callable[[httpx.Response, httpx.Response], TestClient]


@pytest.fixture
def boot(monkeypatch: pytest.MonkeyPatch, respx_allows_unused_routes: None) -> Iterator[Boot]:
    """Boot the real maintenance service with signing configured: `boot(seed, keys)` answers the running client."""
    env = {
        "MAINTENANCE_SECRETS_FROM_DAPR": "true",
        "RASK_SIGNING_IDENTITY": IDENTITY,
        "MAINTENANCE_S3_ACCESS_KEY_ID": "test",
        "MAINTENANCE_LINEAGE_EMIT_ENABLED": "true",
        "DAPR_HTTP_PORT": "3500",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    from maintenance import service
    from maintenance.core.config import get_settings

    monkeypatch.setattr(service, "DaprClient", _Sidecar)
    _Sidecar.published = []
    get_settings.cache_clear()
    with ExitStack() as stack:

        def _boot(seed: httpx.Response, keys: httpx.Response) -> TestClient:
            stack.enter_context(respx.mock)
            respx.get(f"{SECRETS}/lance").mock(return_value=httpx.Response(200, json={"minio-secret-key": "the-s3-secret"}))
            respx.get(f"{SECRETS}/signing-key-{IDENTITY}").mock(return_value=seed)
            respx.get(f"{SECRETS}/signing-public-{IDENTITY}").mock(return_value=keys)
            return stack.enter_context(TestClient(service.app))

        yield _boot
    get_settings.cache_clear()


def _emit(client: TestClient, kind: str) -> None:
    """Emit through the service's own emitter, on the loop its lifespan runs on."""
    emitter = client.app.state.lineage_emitter
    portal = client.portal
    assert portal is not None, "the client was never entered, so the service's lifespan did not run"
    if kind == "failure":
        portal.call(lambda: emitter.emit_maintenance_failed(table_id="acme$events", namespace="acme", error="compaction failed"))
    else:
        portal.call(lambda: emitter.emit_maintenance(table_id="acme$events", namespace="acme"))


@pytest.mark.parametrize(
    ("kind", "event_type"),
    [
        pytest.param("compaction", "COMPLETE", id="a-compaction"),
        pytest.param("failure", "FAIL", id="a-failure"),
    ],
)
def test_every_event_the_sweep_emits_is_authored_and_signed_as_the_service(boot: Boot, event_signer: Any, kind: str, event_type: str) -> None:
    pair = event_signer(IDENTITY)
    client = boot(httpx.Response(200, json={"seed": pair.seed}), httpx.Response(200, json={"keys": pair.public}))

    _emit(client, kind)

    assert len(_Sidecar.published) == 1, "nothing was published"
    event = _Sidecar.published[0]
    verified = verify_signature(event, source=_Published(pair.public), signers=frozenset({IDENTITY}), delegators=frozenset())
    assert (verified.identity, event["run"]["facets"]["author"]["sub"], event["eventType"]) == (IDENTITY, IDENTITY, event_type)


def test_a_maintenance_without_its_key_emits_nothing_and_is_not_ready(boot: Boot) -> None:
    client = boot(httpx.Response(500), httpx.Response(404))

    _emit(client, "failure")
    readiness = client.get("/readyz")

    assert _Sidecar.published == [], f"a signer without its key published {len(_Sidecar.published)} events"
    assert (readiness.status_code, sorted(readiness.json()["components"])) == (503, ["signing"])


RETRY = {"status": "RETRY"}


@pytest.mark.parametrize(
    ("route", "body", "answer"),
    [
        pytest.param("/maintenance-work", {"data": {"not": "a unit"}}, RETRY, id="a-unit-of-work"),
        pytest.param("/maintenance-index", {"data": {"not": "a unit"}}, RETRY, id="an-index-build"),
        pytest.param("/maintenance-arrival", {"data": {"not": "an event"}}, RETRY, id="a-write-arrival"),
        pytest.param("/maintenance-cron", {}, {"status": "skipped", "reason": "signing key unresolved"}, id="the-sweeps-tick"),
    ],
)
def test_every_delivery_that_would_emit_is_answered_before_it_is_read_while_the_key_is_unresolved(
    route: str, body: dict[str, Any], answer: dict[str, str]
) -> None:
    settings = MaintenanceSettings.model_validate(
        {
            "MAINTENANCE_WORK_TOPIC": "maintenance-work",
            "MAINTENANCE_INDEX_TOPIC": "maintenance-index",
            "MAINTENANCE_BINDING_NAME": "maintenance-cron",
            "MAINTENANCE_S3_ACCESS_KEY_ID": "test",
        }
    )
    app = FastAPI()
    app.state.lineage_emitter = NoopEmitter()
    attach_signing(app, SigningKeyHolder(identity=IDENTITY, store="lance-secrets", load_key=SigningKey.from_seed, parse_published=parse_published_keys))
    app.include_router(build_router(settings))
    wrapper = register_work_route(app, settings)
    register_arrival_route(app, settings, wrapper)
    register_index_route(app, settings, wrapper)

    answered = TestClient(app).post(route, json=body)

    assert (answered.status_code, answered.json()) == (200, answer)


def test_an_emitter_refuses_a_key_for_an_identity_it_does_not_stamp_as_its_author() -> None:
    holder = SigningKeyHolder(identity=IDENTITY, store="lance-secrets", load_key=SigningKey.from_seed, parse_published=parse_published_keys)

    with pytest.raises(ValueError, match="signer that is not its event's author"):
        DaprMaintenanceEmitter(
            cast("DaprClient", _Sidecar()),
            pubsub="p",
            topic="lineage.events.v1",
            job_namespace="lance",
            timeout_seconds=1.0,
            author="service-someone-else",
            signing=holder,
        )
