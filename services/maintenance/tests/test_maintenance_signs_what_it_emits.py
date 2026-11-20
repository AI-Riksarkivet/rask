"""Maintenance signs every event it emits as itself, authors its failures, and emits nothing without its key.

[[LH-064]], [[XC-078]]. A verifier refuses an unsigned event, and a signature only verifies when the signer is the event's
author, so the service stamps its own identity as the author of EVERY lineage event it emits and signs as that identity.
That includes a FAIL: it was authorless, which a verifier could never accept, and an unattributed failure is the one a
person is most likely to have to chase. The control event a trash purge announces names the service as its actor and is
signed the same way, with no delegation: maintenance never signs for a person.

A SIGNER WITHOUT ITS KEY EMITS NOTHING and reports itself not ready, and takes no task meanwhile
(`maintenance.services.queue.Worker`, driven on a real broker by the queue's own lane). The signer is the worker's
process, maintenance's only one. Every event names its table's and namespace's uuids beside the names
(ADR 0173 §10), inside what is signed. A pass and the purge emit after the work they describe, so a signature that cannot
be made is a withheld event and never a failed pass; maintenance stages no control event, so nothing signs one later.

The first two claims are driven through what the worker's process starts with (`maintenance.runtime`) behind the
fleet's own probes, so the key reaches the emitters the way it does in the cluster: the secret store, the holder, the
emitters and their author. The secret store is answered by the suite's double, the connection to the bus is a
recording stand-in, and the store session is exchanged at moto's STS.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator, Callable, Iterator, Sequence
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.bus_double import BusDouble, session_holding
from tests.secret_store_double import SecretStoreDouble

from lakehouse.events.control_emit import emit_control
from lakehouse.events.lineage_events import DatasetUuids, dataset_uuids
from lakehouse.events.signing import RefusalReason, SignatureError, SigningKey, parse_published_keys, verify_control_signature, verify_signature
from lakehouse.governed.signing_key import SigningKeyHolder, signing_ready_check
from lakehouse.lifecycle import mark_started
from lakehouse.service_app.factory import build_service_app
from lakehouse.supervision import Supervisor
from maintenance.core.lineage_emit import BusMaintenanceEmitter
from maintenance.services.purge import ACTOR


#: Maintenance's service-account subject: the author it stamps, and the identity it signs as.
IDENTITY = "kubernetes~system:serviceaccount:rask:rask-sa-maintenance"
#: The name its key pair is stored under (`signing_key_name`): the account's name.
KEY_NAME = "rask-sa-maintenance"
#: The uuids the catalog minted for `acme$events` and for `acme`.
UUIDS = DatasetUuids(table_uuid="0d9c6f3e-7b2a-4c1e-9f5d-2a8b3c4d5e6f", namespace_uuid="3f2e1d0c-4b5a-4968-8776-a5b4c3d2e1f0")


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
def bus() -> BusDouble:
    """The connection to the bus the worker's events are published on."""
    return BusDouble()


@pytest.fixture
def boot(  # noqa: PLR0913 — the fixtures a started, signing worker stands on
    monkeypatch: pytest.MonkeyPatch, secret_store: SecretStoreDouble, bus: BusDouble, respx_allows_unused_routes: None, moto_url: str, tmp_path: Path
) -> Iterator[Boot]:
    """Start what the worker's process stands on, with signing configured, behind the probes it serves: `boot(seed, keys)`
    answers the running client.

    Its object-store identity is a projected token exchanged at moto's STS, as the deployed service's is.
    """
    token = tmp_path / "projected-rask-object-store-token"
    token.write_text("projected-for-rask-object-store")
    env = {
        "MAINTENANCE_S3_WEB_IDENTITY_TOKEN_FILE": str(token),
        "MAINTENANCE_S3_STS_ENDPOINT": moto_url,
        "RASK_SIGNING_IDENTITY": IDENTITY,
        "MAINTENANCE_CATALOG_SERVICE_IDENTITY": IDENTITY,
        "MAINTENANCE_LINEAGE_EMIT_ENABLED": "true",
        "MAINTENANCE_CONTROL_EMIT_ENABLED": "true",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    from maintenance import runtime
    from maintenance.core.config import get_settings

    monkeypatch.setattr(runtime, "bus_session", session_holding(bus))
    get_settings.cache_clear()

    @asynccontextmanager
    async def _started(app: FastAPI) -> AsyncGenerator[None]:
        async with Supervisor() as loops, runtime.standing(app, get_settings(), loops), runtime.collaborators(app, get_settings(), loops) as held:
            app.state.held = held
            mark_started(app)
            yield

    worker = build_service_app(
        title="maintenance worker",
        docs_enabled=False,
        audit_enabled=False,
        lifespan=_started,
        log=logging.getLogger(__name__),
        ready_check=signing_ready_check(),
        max_body_bytes=1024,
        request_deadline_seconds=30.0,
    )
    with ExitStack() as stack:

        def _boot(seed: httpx.Response, keys: httpx.Response) -> TestClient:
            secret_store.hold(f"signing-key-{KEY_NAME}", seed)
            secret_store.hold(f"signing-public-{KEY_NAME}", keys)
            return stack.enter_context(TestClient(worker))

        yield _boot
    get_settings.cache_clear()


def _emit(client: TestClient, kind: str) -> None:
    """Emit through the worker's own emitters, on the loop they were built on: a pass's lineage, or the purge's control event."""
    lineage = client.app.state.held.lineage
    control = client.app.state.held.control
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
        portal.call(lambda: lineage.emit_maintenance_failed(table_id="acme$events", error="maintain: failed", uuids=UUIDS))
    else:
        portal.call(lambda: lineage.emit_maintenance(table_id="acme$events", uuids=UUIDS))


def _signed(event: dict[str, Any], public: str) -> tuple[str, str, str, DatasetUuids] | RefusalReason:
    """Who a verifier holding the service's published key finds signed an event, whom the event names, what it says and
    the uuids it names, or why it is refused."""
    source = _Published(public)
    try:
        if "run" in event:
            verified = verify_signature(event, source=source, signers=frozenset({IDENTITY}), delegators=frozenset())
            return verified.identity, event["run"]["facets"]["rask_author"]["sub"], event["eventType"], dataset_uuids(event["outputs"][0])
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
    boot: Boot, bus: BusDouble, event_signer: Any, kind: str, signed: tuple[str, str, str, DatasetUuids]
) -> None:
    pair = event_signer(IDENTITY)
    client = boot(SecretStoreDouble.held({"seed": pair.seed}), SecretStoreDouble.held({"keys": pair.public}))

    _emit(client, kind)

    assert len(bus.events) == 1, "nothing was published"
    assert _signed(bus.events[0], pair.public) == signed


@pytest.mark.parametrize("kind", [pytest.param("failure", id="a-failure"), pytest.param("purge", id="a-trash-purge")])
def test_a_maintenance_without_its_key_emits_nothing_and_is_not_ready(boot: Boot, bus: BusDouble, kind: str) -> None:
    client = boot(httpx.Response(500), httpx.Response(404))

    _emit(client, kind)
    readiness = client.get("/readyz")

    assert bus.sent == [], f"a signer without its key published {len(bus.sent)} events"
    assert (readiness.status_code, sorted(readiness.json()["components"])) == (503, ["signing"])


def test_a_worker_takes_no_task_while_the_connection_its_control_events_go_out_on_is_away(boot: Boot, bus: BusDouble, event_signer: Any) -> None:
    """Nothing stages a control event, so one the purge emitted while the connection was away would be lost: the worker
    waits for the connection as it waits for its key."""
    pair = event_signer(IDENTITY)
    client = boot(SecretStoreDouble.held({"seed": pair.seed}), SecretStoreDouble.held({"keys": pair.public}))
    held = client.app.state.held
    deadline = time.monotonic() + 10
    while not held.ready() and time.monotonic() < deadline:
        time.sleep(0.02)
    takes_tasks_while_up = held.ready()

    bus.down = True

    assert (takes_tasks_while_up, held.ready()) == (True, False)


def test_an_emitter_refuses_a_key_for_an_identity_it_does_not_stamp_as_its_author() -> None:
    holder = SigningKeyHolder(identity=IDENTITY, load_key=SigningKey.from_seed, parse_published=parse_published_keys)

    with pytest.raises(ValueError, match="signer that is not its event's author"):
        BusMaintenanceEmitter(
            BusDouble(),
            topic="lineage.events.v1",
            job_namespace="lance",
            dataset_namespace="lance-namespace://localhost/api/catalog",
            timeout_seconds=1.0,
            author="service-someone-else",
            signing=holder,
        )


@pytest.mark.asyncio
async def test_the_process_closes_the_client_it_asked_the_catalogs_doors_with(boot: Boot) -> None:
    from maintenance import runtime
    from maintenance.core.config import get_settings
    from maintenance.services import credentials

    async with Supervisor() as loops, runtime.standing(FastAPI(), get_settings(), loops):
        asked_with = credentials.door_client()

    assert asked_with.is_closed
    assert credentials.door_client() is not asked_with, "a process that stands again asks with a client that is open"
    credentials.close_door_client()
