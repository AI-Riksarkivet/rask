"""Maintenance signs every event it emits as itself, authors its failures, and emits nothing without its key.

[[LH-064]], [[XC-078]]. A verifier refuses an unsigned event, and a signature only verifies when the signer is the event's
author, so the service stamps its own identity as the author of EVERY lineage event it emits and signs as that identity.
That includes a FAIL: it was authorless, which a verifier could never accept, and an unattributed failure is the one a
person is most likely to have to chase. The control event a trash purge announces names the service as its actor and is
signed the same way, with no delegation: maintenance never signs for a person.

A SIGNER WITHOUT ITS KEY EMITS NOTHING and reports itself not ready, and every delivery that would make it act (a write
arrival, the sweep's own tick) is answered before the delivery is read, so it is redelivered or retried rather than
lost. Every event names its table's and namespace's uuids beside the names (ADR 0173 §10), inside what is signed. The
sweep and the purge emit after the work they describe, so a signature that
cannot be made is a withheld event and never a failed pass; maintenance stages no control event, so nothing signs one
later.

The first two claims are driven through the real service lifespan, so the key reaches the emitters the way it does in the
cluster: the secret store, the holder, the emitters and their author. The sidecar's secret API is answered by respx and
its publish is a recording stand-in.
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

from lineage_kit import RefusalReason, SignatureError, SigningKey, parse_published_keys, verify_control_signature, verify_signature
from maintenance.api.arrival import register_arrival_route
from maintenance.api.routes import build_router
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import DaprMaintenanceEmitter, NoopEmitter
from maintenance.services.purge import ACTOR
from service_kit.control_emit import emit_control
from service_kit.governed.signing_key import SigningKeyHolder, attach_signing
from service_kit.openlineage import DatasetUuids, dataset_uuids


if TYPE_CHECKING:
    from dapr.aio.clients import DaprClient


#: Maintenance's service-account subject: the author it stamps, and the identity it signs as.
IDENTITY = "kubernetes~system:serviceaccount:rask:rask-sa-maintenance"
#: The name its key pair is stored under (`signing_key_name`): the account's name.
KEY_NAME = "rask-sa-maintenance"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"
#: The uuids the catalog minted for `acme$events` and for `acme`.
UUIDS = DatasetUuids(table_uuid="0d9c6f3e-7b2a-4c1e-9f5d-2a8b3c4d5e6f", namespace_uuid="3f2e1d0c-4b5a-4968-8776-a5b4c3d2e1f0")


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
        "MAINTENANCE_CATALOG_SERVICE_IDENTITY": IDENTITY,
        "MAINTENANCE_LINEAGE_EMIT_ENABLED": "true",
        "MAINTENANCE_CONTROL_EMIT_ENABLED": "true",
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
            respx.get(f"{SECRETS}/maintenance-s3").mock(return_value=httpx.Response(200, json={"access_key": "test", "secret_key": "the-s3-secret"}))
            respx.get(f"{SECRETS}/signing-key-{KEY_NAME}").mock(return_value=seed)
            respx.get(f"{SECRETS}/signing-public-{KEY_NAME}").mock(return_value=keys)
            return stack.enter_context(TestClient(service.app))

        yield _boot
    get_settings.cache_clear()


def _emit(client: TestClient, kind: str) -> None:
    """Emit through the service's own emitters, on the loop its lifespan runs on: the sweep's lineage, or the purge's control event."""
    lineage = client.app.state.lineage_emitter
    control = client.app.state.control_emitter
    portal = client.portal
    assert portal is not None, "the client was never entered, so the service's lifespan did not run"
    if kind == "purge":
        portal.call(
            lambda: emit_control(
                control,
                action="table_purged",
                object_type="table",
                object_id="table:acme$events",
                object_uuid=UUIDS.table_uuid,
                namespace_uuid=UUIDS.namespace_uuid,
                actor=ACTOR,
                extra={"reason": "trash_expired"},
            )
        )
    elif kind == "failure":
        portal.call(lambda: lineage.emit_maintenance_failed(table_id="acme$events", namespace="acme", error="maintain: failed", uuids=UUIDS))
    else:
        portal.call(lambda: lineage.emit_maintenance(table_id="acme$events", namespace="acme", uuids=UUIDS))


def _signed(event: dict[str, Any], public: str) -> tuple[str, str, str, DatasetUuids] | RefusalReason:
    """Who a verifier holding the service's published key finds signed an event, whom the event names, what it says and
    the uuids it names, or why it is refused."""
    source = _Published(public)
    try:
        if "run" in event:
            verified = verify_signature(event, source=source, signers=frozenset({IDENTITY}), delegators=frozenset())
            return verified.identity, event["run"]["facets"]["author"]["sub"], event["eventType"], dataset_uuids(event["outputs"][0])
        verified = verify_control_signature(event, source=source, signers=frozenset({IDENTITY}), delegators=frozenset())
    except SignatureError as exc:
        return exc.reason
    return verified.identity, event["actor"], event["action"], DatasetUuids(table_uuid=event["object_uuid"], namespace_uuid=event["namespace_uuid"])


@pytest.mark.parametrize(
    ("kind", "signed"),
    [
        pytest.param("expiry", (IDENTITY, IDENTITY, "COMPLETE", UUIDS), id="an-expiry"),
        pytest.param("failure", (IDENTITY, IDENTITY, "FAIL", UUIDS), id="a-failure"),
        pytest.param("purge", (IDENTITY, ACTOR, "table_purged", UUIDS), id="a-trash-purge"),
    ],
)
def test_every_event_the_service_emits_is_authored_and_signed_as_the_service(
    boot: Boot, event_signer: Any, kind: str, signed: tuple[str, str, str, DatasetUuids]
) -> None:
    pair = event_signer(IDENTITY)
    client = boot(httpx.Response(200, json={"seed": pair.seed}), httpx.Response(200, json={"keys": pair.public}))

    _emit(client, kind)

    assert len(_Sidecar.published) == 1, "nothing was published"
    assert _signed(_Sidecar.published[0], pair.public) == signed


@pytest.mark.parametrize("kind", [pytest.param("failure", id="a-failure"), pytest.param("purge", id="a-trash-purge")])
def test_a_maintenance_without_its_key_emits_nothing_and_is_not_ready(boot: Boot, kind: str) -> None:
    client = boot(httpx.Response(500), httpx.Response(404))

    _emit(client, kind)
    readiness = client.get("/readyz")

    assert _Sidecar.published == [], f"a signer without its key published {len(_Sidecar.published)} events"
    assert (readiness.status_code, sorted(readiness.json()["components"])) == (503, ["signing"])


RETRY = {"status": "RETRY"}


@pytest.mark.parametrize(
    ("route", "body", "answer"),
    [
        pytest.param("/maintenance-arrival", {"data": {"not": "an event"}}, RETRY, id="a-write-arrival"),
        pytest.param("/maintenance-cron", {}, {"status": "skipped", "reason": "signing key unresolved"}, id="the-sweeps-tick"),
    ],
)
def test_every_delivery_that_would_emit_is_answered_before_it_is_read_while_the_key_is_unresolved(
    route: str, body: dict[str, Any], answer: dict[str, str]
) -> None:
    settings = MaintenanceSettings.model_validate(
        {
            "MAINTENANCE_ARRIVAL_ENABLED": "true",
            "MAINTENANCE_BINDING_NAME": "maintenance-cron",
            "MAINTENANCE_S3_ACCESS_KEY_ID": "test",
        }
    )
    app = FastAPI()
    app.state.lineage_emitter = NoopEmitter()
    attach_signing(app, SigningKeyHolder(identity=IDENTITY, store="lance-secrets", load_key=SigningKey.from_seed, parse_published=parse_published_keys))
    app.include_router(build_router(settings))
    register_arrival_route(app, settings)

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
