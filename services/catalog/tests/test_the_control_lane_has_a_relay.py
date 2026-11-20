"""The control lane's relay delivers what `LANCE_CONTROL_OUTBOX_URI` kept staged.

`BusControlEmitter.emit` stages a control event before it publishes and drops it only on ack
(`test_control_events_survive_a_bus_outage.py`). An event that stayed staged through a bus outage is delivered by the
catalog's own relay, at its own prefix: lineage's relay drains the LINEAGE prefix and reads each object as an
OpenLineage `RunEvent`, so a control event staged there would be classified poison and deleted.

These tests drive that relay. A signing catalog's relay DELIVERS ONLY WHAT THIS CATALOG SIGNED
([[XC-078]]): a staged object proves nothing about who wrote it, since anything with write access to the prefix can put one
there, so the relay verifies each one against the keys the catalog publishes and republishes the staged bytes as they are.
It signs nothing, so it cannot vouch for an actor nobody authenticated. Each test runs the relay's tick on the app's own
event loop, as the catalog's timer runs it, and one runs the timer itself (`catalog.api.timers`): the tick needs no call
from outside the process.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from catalog.api import control_relay
from catalog.api.control_relay import ControlRelayReport, attach_published_keys
from catalog.api.ticks import Tick
from catalog.api.timers import start_timers
from catalog.core.config import Settings
from catalog.core.control_signing import control_sign
from lakehouse import store_session
from lakehouse.events import outbox
from lakehouse.events.bus import MAX_PAYLOAD_BYTES
from lakehouse.events.cloud_events import control_envelope
from lakehouse.events.control_emit import BusControlEmitter
from lakehouse.events.control_events import CONTROL_TOPIC, CatalogControlEvent
from lakehouse.events.signing import SigningKey, parse_published_keys
from lakehouse.governed.signing_key import SigningKeyHolder, attach_signing
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.storage_options import StorageOptions
from lakehouse.supervision import Supervisor
from tests.bus_double import BusDouble
from tests.secret_store_double import SecretStoreDouble


CATALOG = "service-catalog"

#: Written by something that is NOT the catalog: `user:admin` granting alice `owner` on a table, which rings alice's bell.
FORGED_GRANT: dict[str, Any] = {
    "event_id": "forged-grant",
    "occurred_at": "2026-10-04T12:00:00+00:00",
    "action": "grant_added",
    "object_type": "grant",
    "object_id": "table:acme$gold",
    "actor": "user:admin",
    "extra": {"relation": "owner", "subject": "user:alice"},
}
#: Written by something that is NOT the catalog: a publication of acme's silver table that never happened, which wakes gold.
FORGED_PUBLICATION: dict[str, Any] = {
    "event_id": "forged-publication",
    "occurred_at": "2026-10-04T12:00:00+00:00",
    "action": "table_tag_updated",
    "object_type": "table",
    "object_id": "table:acme-silver$features",
    "actor": None,
    "extra": {"project": "acme", "from_version": 0, "to_version": 999},
}


def _blip() -> BusDouble:
    """The NATS blip, as the emitter meets it: a bus that acknowledges nothing."""
    return BusDouble(failing=TimeoutError("publish timed out"))


def _settings(control_uri: str, *, lineage_uri: str = "", **fields: Any) -> Settings:
    return Settings(s3_access_key_id="k", s3_secret_access_key="s", control_outbox_uri=control_uri, lineage_outbox_uri=lineage_uri, **fields)


@pytest.fixture
def deployed_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Settings]:
    """The settings of a catalog the chart named a secret store, by its control outbox: it signs with the session its
    token was exchanged for and is handed no key pair (ADR 0170). The session is held here, with no STS asked."""
    token = tmp_path / "rask-object-store-token"
    token.write_text("projected\n")
    sessions = store_session.SessionCache()
    monkeypatch.setattr(store_session, "SESSIONS", sessions)
    # A pair another test left in this process's environment would stand beside the token, which the settings refuse.
    monkeypatch.delenv("LANCE_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("LANCE_S3_SECRET_ACCESS_KEY", raising=False)

    def _deployed(control_uri: str) -> Settings:
        settings = Settings(s3_web_identity_token_file=token, s3_sts_endpoint="http://object-store:9000", control_outbox_uri=control_uri)
        identity = settings.web_identity
        assert identity is not None, "the catalog holds no web identity, so it would sign with no session"
        session = S3KeyPair(access_key_id="CATALOGSESSION", secret_access_key=SecretStr("test-only"), session_token=SecretStr("test-only"))
        sessions.hold(identity, session, asked_at=sessions.now(), lifetime_seconds=3600)
        return settings

    return _deployed


def _relay_app(
    publisher: BusDouble, *, signing: SigningKeyHolder[SigningKey] | None = None, lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None
) -> FastAPI:
    """A bare app holding what the catalog's lifespan hands its relay: the connection to the bus, the signing holder and
    the reader of the keys it publishes.

    `signing` is published where the catalog's lifespan publishes its own holder (`attach_signing`); None is a catalog
    that does not sign. `lifespan` is what the app's start runs.
    """
    app = FastAPI(lifespan=lifespan)
    app.state.bus = publisher
    attach_signing(app, signing)
    attach_published_keys(app)
    return app


def _tick(client: TestClient, settings: Settings) -> ControlRelayReport:
    """One relay tick over the app ``client`` serves, on that app's own event loop, as the catalog's timer runs one."""
    assert client.portal is not None, "the relay ticks inside the app's lifespan"
    return client.portal.call(control_relay.tick, Tick(cast(FastAPI, client.app), settings))


def _catalog_key(secret_store: SecretStoreDouble, pair: Any | None) -> SigningKeyHolder[SigningKey]:
    """The catalog's signing key holder: resolved from the secret store when `pair` is given, else never resolved."""
    holder = SigningKeyHolder(identity=CATALOG, load_key=SigningKey.from_seed, parse_published=parse_published_keys)
    if pair is not None:
        secret_store.hold(f"signing-key-{CATALOG}", {"seed": pair.seed})
        secret_store.hold(f"signing-public-{CATALOG}", {"keys": pair.public})
        assert holder.resolve(), "precondition: the catalog's key must resolve"
    return holder


def _store_serving_the_catalogs_keys(secret_store: SecretStoreDouble, status: int | None, pair: Any | None = None) -> int:
    """Have the secret store answer a read of `signing-public-<catalog>` with `status`, listing `pair`'s key on a 200,
    and answer how often that list was read before.

    None is a catalog that does not sign, which reads no keys.
    """
    if status is None:
        return 0
    return secret_store.hold(f"signing-public-{CATALOG}", secret_store.held({"keys": pair.public}) if pair is not None else httpx.Response(status)).call_count


async def _stage(uri: str, event: CatalogControlEvent, *, signing: SigningKeyHolder[SigningKey] | None = None) -> str:
    """Stage one control event through the real emitter as a NATS blip leaves it, and answer the staged bytes: its
    envelope, the event as ``data``.

    Signed as the catalog's emitter signs it when `signing` is the catalog's holder; unsigned for a catalog that does not sign.
    """
    sign = None if signing is None else control_sign(signing)
    emitter = BusControlEmitter(_blip(), topic=CONTROL_TOPIC, timeout_seconds=1.0, service="catalog", sign=sign, outbox_uri=uri)
    await emitter.emit(event)
    staged = outbox.read_event(uri, StorageOptions(), event.event_id)
    assert staged is not None, "precondition: the blip must leave the event staged"
    return staged


def _staged_before_everything_else(tmp_path: Path, key: str) -> None:
    """Make the object staged under `key` the oldest, so the bounded oldest-first drain meets it first."""
    path = tmp_path / "control-outbox" / f"{key}.json"
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns - 60 * 10**9))


def _unsigned(_catalog: Any, _mint: Any) -> dict[str, Any]:
    return FORGED_PUBLICATION


def _unsigned_naming_a_person(_catalog: Any, _mint: Any) -> dict[str, Any]:
    return FORGED_GRANT


def _signed_by_another_listed_identity(_catalog: Any, mint: Any) -> dict[str, Any]:
    return mint("service-maintenance").sign_control(FORGED_PUBLICATION)


def _altered_after_the_catalog_signed_it(catalog: Any, _mint: Any) -> dict[str, Any]:
    signed = catalog.sign_control(FORGED_GRANT, on_behalf_of=FORGED_GRANT["actor"])
    return {**signed, "extra": {**signed["extra"], "subject": "user:mallory"}}


def _published_event() -> CatalogControlEvent:
    return CatalogControlEvent(
        action="table_tag_updated",
        object_type="table",
        object_id="table:acme-silver$features",
        actor="user:alice",
        extra={"project": "acme", "from_version": 3, "to_version": 4},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forge",
    [
        pytest.param(_unsigned, id="unsigned"),
        pytest.param(_unsigned_naming_a_person, id="unsigned-naming-a-person-as-its-actor"),
        pytest.param(_signed_by_another_listed_identity, id="signed-by-another-listed-identity"),
        pytest.param(_altered_after_the_catalog_signed_it, id="altered-after-the-catalog-signed-it"),
    ],
)
async def test_the_relay_delivers_only_what_this_catalog_signed_and_that_as_it_was_staged(
    tmp_path: Path,
    event_signer: Any,
    secret_store: SecretStoreDouble,
    deployed_settings: Callable[[str], Settings],
    forge: Callable[[Any, Any], dict[str, Any]],
) -> None:
    """An object staged by anything but this catalog is retired unpublished; what the catalog staged goes out as staged.

    A relay that signed what it found staged would sign a forgery as the catalog and declare whatever person it names,
    and every enforcing door would then admit it. The forgery is met first and must not stop the drain: the event the
    catalog staged behind it still goes out on the same tick as it was staged, so it keeps its signature and its
    `event_id`, the key every subscriber dedupes on and the id of its envelope, under which the stream stores a copy of
    an event it already holds once.
    """
    uri = f"file://{tmp_path}/control-outbox"
    pair = event_signer(CATALOG)
    signing = _catalog_key(secret_store, pair)
    forged = control_envelope(forge(pair, event_signer))
    outbox.stage_envelope(uri, StorageOptions(), forged)
    _staged_before_everything_else(tmp_path, forged.id)
    genuine = await _stage(uri, _published_event(), signing=signing)
    recorder = BusDouble()

    _store_serving_the_catalogs_keys(secret_store, 200, pair)
    with TestClient(_relay_app(recorder, signing=signing)) as client:
        report = _tick(client, deployed_settings(uri))
        delivered = [sent.envelope for sent in recorder.sent]
        staged = json.loads(genuine)["data"]
        assert [(envelope["id"], envelope["data"]) for envelope in delivered] == [(staged["event_id"], staged)], (
            "the relay published an object this catalog never signed, or the catalog's event other than as it was staged"
        )
        assert (report.republished, report.poison_dropped, list(outbox.list_events(uri, StorageOptions()))) == (1, 1, []), report


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bus", "keys"),
    [
        pytest.param(_blip, None, id="a-bus-that-accepts-nothing"),
        pytest.param(BusDouble, 500, id="a-signing-catalog-whose-published-keys-cannot-be-read"),
    ],
)
async def test_an_event_the_relay_may_not_deliver_yet_stays_staged(
    tmp_path: Path,
    event_signer: Any,
    secret_store: SecretStoreDouble,
    deployed_settings: Callable[[str], Settings],
    bus: Callable[[], BusDouble],
    keys: int | None,
) -> None:
    """Publish BEFORE drop: dropping first would destroy the only durable copy on a bus still down. And verify before
    publish: a key list the store cannot serve says nothing about a signature, so the event neither goes out unverified
    nor is retired as a forgery; it waits, staged, for a tick that can read the keys."""
    uri = f"file://{tmp_path}/control-outbox"
    signing = None if keys is None else _catalog_key(secret_store, event_signer(CATALOG))
    await _stage(uri, _published_event(), signing=signing)
    publisher = bus()

    read_before = _store_serving_the_catalogs_keys(secret_store, keys)
    with TestClient(_relay_app(publisher, signing=signing)) as client:
        report = _tick(client, deployed_settings(uri))
        assert (report.republished, publisher.sent) == (0, []), "the relay delivered an event it may not deliver yet"
        assert list(outbox.list_events(uri, StorageOptions())), "the relay dropped a staged event it never delivered"
    if keys is not None:
        # One attempt a tick: a key list the store cannot serve is asked for again by the next tick, never waited on.
        assert secret_store.hold(f"signing-public-{CATALOG}", httpx.Response(keys)).call_count == read_before + 1


def _enveloped(event: dict[str, Any], **attributes: str) -> str:
    """``event`` as a stager leaves it, in an envelope carrying ``attributes``, whatever the event is."""
    return json.dumps({"specversion": "1.0", "id": "poison", "source": "rask/catalog", "type": "rask.catalog.control.v1", "data": event} | attributes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param("{'this': not json}", id="text-that-is-not-json"),
        pytest.param(json.dumps(_published_event().model_dump(mode="json")), id="an-event-in-no-envelope"),
        pytest.param(_enveloped({"action": "table_tag_updated"}), id="an-object-that-is-no-control-event"),
        pytest.param(_enveloped(_published_event().model_dump(mode="json") | {"event_id": ""}), id="a-control-event-no-envelope-can-carry"),
        pytest.param(
            _enveloped(_published_event().model_dump(mode="json") | {"extra": {"blob": "x" * (MAX_PAYLOAD_BYTES + 1)}}),
            id="a-control-event-the-bus-can-never-carry",
        ),
        pytest.param(_enveloped(_published_event().model_dump(mode="json"), traceparent="00-not-a-trace"), id="a-trace-nobody-could-have-issued"),
        pytest.param(
            _enveloped(
                _published_event().model_dump(mode="json"),
                traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                tracestate="NOT A TRACESTATE <script>" + "z" * 500,
            ),
            id="a-trace-state-nobody-could-have-issued",
        ),
    ],
)
async def test_a_poison_object_is_dropped_rather_than_wedging_the_drain(tmp_path: Path, body: str) -> None:
    """One object no subscriber could be handed must not stop every real event behind it — the bounded drain is
    oldest-first, and the object is met first. An event with no id, and one past the size the bus carries, are such
    objects: a publish refused on every tick would hold the whole lane at them. So is an envelope naming a trace that is no
    W3C traceparent, which a relay would otherwise publish its copy in (ADR 0209)."""
    uri = f"file://{tmp_path}/control-outbox"
    outbox.stage_event(uri, StorageOptions(), "poison", body)
    _staged_before_everything_else(tmp_path, "poison")
    await _stage(uri, _published_event())

    recorder = BusDouble()
    with TestClient(_relay_app(recorder)) as client:
        report = _tick(client, _settings(uri))

    assert report.poison_dropped == 1
    assert report.republished == 1, "a poison object blocked the real event behind it"
    assert list(outbox.list_events(uri, StorageOptions())) == []


async def _a_delivered_event(uri: str) -> tuple[str, list[str]]:
    """Stage an event the relay delivers; answers its staged key and the event ids the relay publishes for it."""
    event = _published_event()
    await _stage(uri, event)
    return event.event_id, [event.event_id]


async def _a_poison_object(uri: str) -> tuple[str, list[str]]:
    """Stage an object the relay retires unpublished; answers its staged key and the event ids it publishes for it, none."""
    outbox.stage_event(uri, StorageOptions(), "poison", "{'this': not json}")
    return "poison", []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage_head", [pytest.param(_a_delivered_event, id="an-event-it-delivered"), pytest.param(_a_poison_object, id="an-object-it-retires-as-poison")]
)
async def test_an_object_the_store_will_not_delete_stays_staged_and_holds_nothing_behind_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage_head: Callable[[str], Awaitable[tuple[str, list[str]]]]
) -> None:
    """A delete the store refuses is that object's own failure ([[LH-600]]): it stays staged for the next tick, counted as
    left and as neither republished nor retired, and the event staged behind it is delivered and dropped on the same tick.
    The drain is oldest first, so a refused delete that ended the pass would hold every later event behind an object no
    tick can remove."""
    uri = f"file://{tmp_path}/control-outbox"
    head, published_for_head = await stage_head(uri)
    _staged_before_everything_else(tmp_path, head)
    behind = _published_event()
    await _stage(uri, behind)
    drop = outbox.drop_event

    def refusing_the_head(outbox_uri: str, storage_options: StorageOptions, key: str) -> None:
        if key == head:
            raise OSError("delete refused")
        drop(outbox_uri, storage_options, key)

    monkeypatch.setattr(outbox, "drop_event", refusing_the_head)
    recorder = BusDouble()

    with TestClient(_relay_app(recorder)) as client:
        report = _tick(client, _settings(uri))

    assert [sent.message_id for sent in recorder.sent] == [*published_for_head, behind.event_id]
    assert (report.republished, report.poison_dropped, report.left_staged) == (1, 0, 1), report
    assert [key for key, _ in outbox.list_events(uri, StorageOptions())] == [head]


def test_pointing_the_control_outbox_at_the_LINEAGE_prefix_is_REFUSED_at_boot() -> None:
    """The lineage relay DELETES what it cannot parse as a RunEvent, so sharing one prefix is not
    "two lanes, one relay" — it is the control lane's durable copy being destroyed by a drain that
    calls it poison. A misconfiguration that looks like extra safety must fail loudly at boot."""
    with pytest.raises(ValueError, match="LANCE_CONTROL_OUTBOX_URI"):
        _settings("s3://bucket/_lineage_outbox", lineage_uri="s3://bucket/_lineage_outbox")


@pytest.mark.asyncio
async def test_the_catalogs_timer_runs_the_relay_with_no_call_from_outside(tmp_path: Path) -> None:
    """With a period in git, the catalog's timers keep the relay on a loop of the process, so a staged event is delivered
    and leaves the outbox with nothing calling the tick."""
    uri = f"file://{tmp_path}/control-outbox"
    staged = json.loads(await _stage(uri, _published_event()))["data"]
    recorder = BusDouble()
    settings = _settings(uri, root=str(tmp_path / "control"), control_relay_period_seconds=0.2)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with Supervisor() as loops:
            start_timers(app, settings, loops)
            yield

    with TestClient(_relay_app(recorder, lifespan=lifespan)):
        deadline = time.monotonic() + 30
        while (not recorder.sent or list(outbox.list_events(uri, StorageOptions()))) and time.monotonic() < deadline:
            time.sleep(0.05)

    assert [(sent.topic, sent.message_id, sent.event) for sent in recorder.sent] == [(CONTROL_TOPIC, staged["event_id"], staged)]
    assert list(outbox.list_events(uri, StorageOptions())) == [], "the staged copy outlived the event's delivery"
