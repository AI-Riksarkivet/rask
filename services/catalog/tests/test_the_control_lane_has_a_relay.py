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

These tests drive the relay that closes it. Each one fails with an ImportError until it exists, which
is the honest shape of "there is no control-lane relay".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.core.config import Settings
from service_kit.control_emit import DaprControlEmitter
from service_kit.control_events import CONTROL_TOPIC, CatalogControlEvent
from service_kit.lakehouse import outbox


BINDING = "catalog-control-relay-cron"


class _Blip:
    """The NATS blip, as the emitter meets it: a sidecar that accepts nothing."""

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


def _relay_app(settings: Settings, publisher: object) -> FastAPI:
    """A bare app carrying ONLY the relay router, so the route's own path is what is under test."""
    from catalog.api.control_relay import mount_control_relay
    from catalog.core.config import get_settings

    app = FastAPI()
    mounted = mount_control_relay(app, settings.control_relay_binding_name)
    assert mounted, "the relay router refused to mount"
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.dapr_client = publisher
    return app


async def _stage(uri: str, event: CatalogControlEvent) -> None:
    """Stage one control event the way a NATS blip does — through the real emitter."""
    emitter = DaprControlEmitter(cast("Any", _Blip()), pubsub="p", topic=CONTROL_TOPIC, timeout_seconds=1.0, service="catalog", outbox_uri=uri)
    await emitter.emit(event)


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
async def test_the_relay_republishes_the_STAGED_BYTES_so_the_cascade_dedupes(tmp_path: Path) -> None:
    """`event_id` is the cascade's idempotency key: `/publication-arrival` mints its stage token from it
    and `stage_submission_id` hashes that into the deterministic workflow instance id. Re-minting the
    event — or round-tripping it through the model — would produce a NEW id and drive the hop twice."""
    uri = f"file://{tmp_path}/control-outbox"
    event = _published_event()
    await _stage(uri, event)

    recorder = _Recorder()
    with TestClient(_relay_app(_settings(uri), recorder)) as client:
        client.post(f"/{BINDING}")

    delivered = json.loads(recorder.published[0]["data"])
    assert delivered["event_id"] == event.event_id, "the re-published event carries a different id — the cascade cannot dedupe it"
    assert delivered["extra"] == {"project": "acme", "from_version": 3, "to_version": 4}, "the range the mover reads did not survive the relay"


@pytest.mark.asyncio
async def test_a_FAILED_republish_leaves_the_event_staged(tmp_path: Path) -> None:
    """Publish BEFORE drop. Dropping first would destroy the only durable copy on a bus still down."""
    uri = f"file://{tmp_path}/control-outbox"
    await _stage(uri, _published_event())

    with TestClient(_relay_app(_settings(uri), _Blip())) as client:
        response = client.post(f"/{BINDING}")

    assert response.json()["republished"] == 0
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
