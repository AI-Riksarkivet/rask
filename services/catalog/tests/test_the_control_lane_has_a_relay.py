"""`LANCE_CONTROL_OUTBOX_URI` stages `table_published`, and NOTHING on the estate republished it.

The control lane got the durable half of the outbox (`DaprControlEmitter.emit` stages before it
publishes and drops only on ack — `test_control_events_survive_a_bus_outage.py` proves that much) and
never got the delivery half. A grep for `outbox.list_events` / `outbox.drop_event` reached exactly one
consumer, `services/lineage/api/reconcile_cron.py`, which drains the LINEAGE prefix and re-ingests each
staged blob as an OpenLineage `RunEvent`.

So the staged copy was a copy nothing read. Two ways that lands, both silent:

* configured at its own prefix, a `table_published` survives the NATS blip and sits there forever —
  and `table_published` is the ONLY thing that wakes silver->gold, so the cascade stops with every pod
  green and nothing red;
* pointed at the LINEAGE prefix so that "a relay drains it", every `CatalogControlEvent` fails
  `RunEvent.model_validate_json` in that drain, is classified POISON and is DELETED
  (`reconcile_cron.py` `_drain_outbox`) — the staged copy is destroyed by the thing meant to save it.

These tests drive the relay that closes it. A signing catalog's relay DELIVERS ONLY WHAT THIS CATALOG SIGNED
([[XC-078]]): a staged object proves nothing about who wrote it, since anything with write access to the prefix can put one
there, so the relay verifies each one against the keys the catalog publishes and republishes the staged bytes as they are.
It signs nothing, so it cannot vouch for an actor nobody authenticated.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.core.config import Settings
from catalog.core.control_signing import control_sign
from lineage_kit import SigningKey, parse_published_keys
from service_kit.control_emit import DaprControlEmitter
from service_kit.control_events import CONTROL_TOPIC, CatalogControlEvent
from service_kit.governed.signing_key import SigningKeyHolder, attach_signing
from service_kit.lakehouse import outbox


BINDING = "catalog-control-relay-cron"
CATALOG = "service-catalog"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"

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
    "action": "table_published",
    "object_type": "table",
    "object_id": "table:acme-silver$features",
    "actor": None,
    "extra": {"project": "acme", "from_version": 0, "to_version": 999},
}


class _Blip:
    """The NATS blip, as the emitter meets it: a sidecar that accepts nothing."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, **_kw: Any) -> None:
        raise TimeoutError("publish timed out")


class _Recorder:
    """A sidecar that accepts everything and remembers exactly what it was handed."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, **kw: Any) -> None:
        self.published.append(kw)


def _settings(control_uri: str, *, lineage_uri: str = "") -> Settings:
    return Settings(
        s3_access_key_id="k",
        s3_secret_access_key="s",
        control_outbox_uri=control_uri,
        lineage_outbox_uri=lineage_uri,
        control_relay_binding_name=BINDING,
    )


def _relay_app(settings: Settings, publisher: object, *, signing: SigningKeyHolder[SigningKey] | None = None) -> FastAPI:
    """A bare app carrying ONLY the relay router, so the route's own path is what is under test.

    `signing` is published where the catalog's lifespan publishes its own holder (`attach_signing`); None is a catalog
    that does not sign.
    """
    from catalog.api.control_relay import mount_control_relay
    from catalog.core.config import get_settings

    app = FastAPI()
    mounted = mount_control_relay(app, settings.control_relay_binding_name)
    assert mounted, "the relay router refused to mount"
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.dapr_client = publisher
    attach_signing(app, signing)
    return app


def _catalog_key(pair: Any | None) -> SigningKeyHolder[SigningKey]:
    """The catalog's signing key holder: resolved from the sidecar's secret API when `pair` is given, else never resolved."""
    holder = SigningKeyHolder(identity=CATALOG, store="lance-secrets", load_key=SigningKey.from_seed, parse_published=parse_published_keys)
    if pair is not None:
        with respx.mock:
            respx.get(f"{SECRETS}/signing-key-{CATALOG}").mock(return_value=httpx.Response(200, json={"seed": pair.seed}))
            respx.get(f"{SECRETS}/signing-public-{CATALOG}").mock(return_value=httpx.Response(200, json={"keys": pair.public}))
            assert holder.resolve(), "precondition: the catalog's key must resolve"
    return holder


@contextmanager
def _sidecar_serving_the_catalogs_keys(status: int | None, pair: Any | None = None) -> Iterator[None]:
    """The catalog's sidecar answering a read of `signing-public-<catalog>` with `status`, listing `pair`'s key on a 200.

    None is a catalog that does not sign, which reads no keys.
    """
    if status is None:
        yield
        return
    with respx.mock:
        body = {"keys": pair.public} if pair is not None else None
        respx.get(f"{SECRETS}/signing-public-{CATALOG}").mock(return_value=httpx.Response(status, json=body))
        yield


async def _stage(uri: str, event: CatalogControlEvent, *, signing: SigningKeyHolder[SigningKey] | None = None) -> str:
    """Stage one control event through the real emitter as a NATS blip leaves it, and answer the staged bytes.

    Signed as the catalog's emitter signs it when `signing` is the catalog's holder; unsigned for a catalog that does not sign.
    """
    sign = None if signing is None else control_sign(signing)
    emitter = DaprControlEmitter(cast("Any", _Blip()), pubsub="p", topic=CONTROL_TOPIC, timeout_seconds=1.0, service="catalog", sign=sign, outbox_uri=uri)
    await emitter.emit(event)
    staged = outbox.read_event(uri, {}, event.event_id)
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
        action="table_published",
        object_type="table",
        object_id="table:acme-silver$features",
        actor="user:alice",
        extra={"project": "acme", "from_version": 3, "to_version": 4},
    )


@pytest.mark.asyncio
async def test_the_relay_REPUBLISHES_a_staged_table_published(tmp_path: Path) -> None:
    """THE WEDGE. Without this the staged event is durable and undelivered, which is not durability."""
    uri = f"file://{tmp_path}/control-outbox"
    event = _published_event()
    await _stage(uri, event)
    assert list(outbox.list_events(uri, {})), "precondition: the blip must leave the event staged"

    recorder = _Recorder()
    with TestClient(_relay_app(_settings(uri), recorder)) as client:
        response = client.post(f"/{BINDING}")

    assert response.status_code == 200, response.text
    assert response.json()["republished"] == 1
    assert [p["topic_name"] for p in recorder.published] == [CONTROL_TOPIC], (
        "the staged control event was not re-published onto the control topic — silver->gold is still asleep"
    )
    assert list(outbox.list_events(uri, {})) == [], "a delivered event must be dropped, or the relay republishes it forever"


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
async def test_the_relay_delivers_only_what_this_catalog_signed_and_that_byte_for_byte(
    tmp_path: Path, event_signer: Any, monkeypatch: pytest.MonkeyPatch, forge: Callable[[Any, Any], dict[str, Any]]
) -> None:
    """An object staged by anything but this catalog is retired unpublished; what the catalog staged goes out as staged.

    A relay that signed what it found staged would sign a forgery as the catalog and declare whatever person it names,
    and every enforcing door would then admit it. The forgery is met first and must not stop the drain: the event the
    catalog staged behind it still goes out on the same tick, byte for byte, so it keeps its signature and its `event_id`,
    the cascade's idempotency key (`/publication-arrival` mints its stage token from it and `stage_submission_id` hashes
    that into the deterministic workflow instance id, so a re-minted id would drive the hop twice).
    """
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    uri = f"file://{tmp_path}/control-outbox"
    pair = event_signer(CATALOG)
    signing = _catalog_key(pair)
    outbox.stage_event(uri, {}, "forged", json.dumps(forge(pair, event_signer)))
    _staged_before_everything_else(tmp_path, "forged")
    genuine = await _stage(uri, _published_event(), signing=signing)
    recorder = _Recorder()

    with _sidecar_serving_the_catalogs_keys(200, pair), TestClient(_relay_app(_settings(uri), recorder, signing=signing)) as client:
        report = client.post(f"/{BINDING}").json()
        assert [published["data"] for published in recorder.published] == [genuine], "the relay published an object this catalog never signed"
        assert (report["republished"], report["poison_dropped"], list(outbox.list_events(uri, {}))) == (1, 1, []), report


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bus", "keys"),
    [
        pytest.param(_Blip, None, id="a-bus-that-accepts-nothing"),
        pytest.param(_Recorder, 500, id="a-signing-catalog-whose-published-keys-cannot-be-read"),
    ],
)
async def test_an_event_the_relay_may_not_deliver_yet_stays_staged(
    tmp_path: Path, event_signer: Any, monkeypatch: pytest.MonkeyPatch, bus: type[_Blip] | type[_Recorder], keys: int | None
) -> None:
    """Publish BEFORE drop: dropping first would destroy the only durable copy on a bus still down. And verify before
    publish: a key list the sidecar cannot serve says nothing about a signature, so the event neither goes out unverified
    nor is retired as a forgery; it waits, staged, for a tick that can read the keys."""
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    uri = f"file://{tmp_path}/control-outbox"
    signing = None if keys is None else _catalog_key(event_signer(CATALOG))
    await _stage(uri, _published_event(), signing=signing)
    publisher = bus()

    with _sidecar_serving_the_catalogs_keys(keys), TestClient(_relay_app(_settings(uri), publisher, signing=signing)) as client:
        response = client.post(f"/{BINDING}")
        assert (response.json()["republished"], publisher.published) == (0, []), "the relay delivered an event it may not deliver yet"
        assert list(outbox.list_events(uri, {})), "the relay dropped a staged event it never delivered"


@pytest.mark.asyncio
async def test_a_poison_object_is_dropped_rather_than_wedging_the_drain(tmp_path: Path) -> None:
    """One unparseable object must not stop every real event behind it — the bounded drain is oldest-first."""
    uri = f"file://{tmp_path}/control-outbox"
    outbox.stage_event(uri, {}, "not-a-control-event", "{'this': not json}")
    await _stage(uri, _published_event())

    recorder = _Recorder()
    with TestClient(_relay_app(_settings(uri), recorder)) as client:
        report = client.post(f"/{BINDING}").json()

    assert report["poison_dropped"] == 1
    assert report["republished"] == 1, "a poison object blocked the real event behind it"
    assert list(outbox.list_events(uri, {})) == []


def test_pointing_the_control_outbox_at_the_LINEAGE_prefix_is_REFUSED_at_boot() -> None:
    """The lineage relay DELETES what it cannot parse as a RunEvent, so sharing one prefix is not
    "two lanes, one relay" — it is the control lane's durable copy being destroyed by a drain that
    calls it poison. A misconfiguration that looks like extra safety must fail loudly at boot."""
    with pytest.raises(ValueError, match="LANCE_CONTROL_OUTBOX_URI"):
        _settings("s3://bucket/_lineage_outbox", lineage_uri="s3://bucket/_lineage_outbox")


def test_the_binding_name_IS_the_served_path(tmp_path: Path) -> None:
    """A Dapr input binding is delivered to POST /<component-name> at the POD ROOT. A component named
    one thing and a route mounted at another is a cron that fires into a 404 on every tick."""
    settings = _settings(f"file://{tmp_path}/control-outbox")
    app = _relay_app(settings, _Recorder())
    binding = settings.control_relay_binding_name

    assert f"/{binding}" in app.openapi()["paths"], f"the relay serves no route at /{binding} — the cron Component ticks into a 404"
    with TestClient(app) as client:
        assert client.options(f"/{binding}").status_code == 200, (
            "Dapr's OPTIONS binding-discovery pre-flight is unanswered — the sidecar logs the binding as not consumed and never delivers"
        )


def test_an_unconfigured_outbox_mounts_NO_relay_route() -> None:
    """Opt-in, exactly like the lineage reconcile route: no binding name, no always-live cron door."""
    from catalog.api.control_relay import mount_control_relay

    app = FastAPI()
    assert mount_control_relay(app, "") is False
    assert not [path for path in app.openapi()["paths"] if path.endswith("relay-cron")]
