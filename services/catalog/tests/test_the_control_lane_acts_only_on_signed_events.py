"""[[CTL-32]] The catalog's own `catalog.control.v1` subscription acts only on what a control signer signed (ADR 0171).

Every catalog replica subscribes to the lane as a broadcast and, for each delivery, appends the event to the change feed
`GET /management/v1/events` serves the console and evicts the bindings it names from the warehouse-binding cache. The
`dapr-api-token` on a delivery proves only that this app's sidecar delivered it, and every app the bus lets publish
reaches that sidecar, so the actor an event names is a claim until its signature proves it. Under an enforcing door a
forgery is acknowledged and acted on nowhere, as notifications' `/control-events` refuses it, while the signed event
delivered after it still reaches both.

Driven over HTTP through the catalog's own routes: the subscription `register_control_dapr` mounts and the feed's poll
door. The keys are read through the sidecar's secret API, which respx answers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.core.config import Settings, get_settings
from catalog.core.control_buffer import ControlEventBuffer
from service_kit.governed.signing_key import signing_public_secret


CATALOG = "kubernetes~system:serviceaccount:default:rask-sa-catalog"
MAINTENANCE = "kubernetes~system:serviceaccount:default:rask-sa-maintenance"
APP_TOKEN = "sidecar-token"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"

#: Deletes the warehouse `acme` is bound to: acted on, it drops `acme` from every replica's binding cache.
FORGED_DELETE: dict[str, Any] = {
    "event_id": "forged-delete",
    "occurred_at": "2026-10-18T12:00:00+00:00",
    "action": "warehouse_deleted",
    "object_type": "warehouse",
    "object_id": "warehouse:wh-acme",
    "actor": "user:dex~mallory",
    "extra": {"namespaces_dropped": ["acme"], "project": "acme"},
}

#: What the catalog itself announced: `beta` was dropped, named by its uuid and its name (ADR 0173 §8).
GENUINE_DROP: dict[str, Any] = {
    "event_id": "genuine-drop",
    "occurred_at": "2026-10-18T12:00:01+00:00",
    "action": "namespace_dropped",
    "object_type": "namespace",
    "object_id": "namespace:7d2e4c19-8a3b-4e6f-b1d0-5c9a2e8f3b47",
    "object_name": "beta",
    "actor": "user:dex~alice",
    "extra": {},
}


def _unsigned(_catalog: Any, _maintenance: Any) -> dict[str, Any]:
    return FORGED_DELETE


def _signed_by_a_signer_whose_role_is_not_this_actions(_catalog: Any, maintenance: Any) -> dict[str, Any]:
    return maintenance.sign_control(FORGED_DELETE)


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch, event_signer: Any) -> Iterator[tuple[TestClient, dict[str, Any], Any, Any]]:
    """A catalog app carrying the subscription and the feed, its cache holding `acme` and `beta`, and both signers."""
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("LANCE_CONTROL_EMIT_ENABLED", "true")
    monkeypatch.setenv("APP_API_TOKEN", APP_TOKEN)
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    get_settings.cache_clear()
    catalog, maintenance = event_signer(CATALOG), event_signer(MAINTENANCE)
    settings = Settings.model_validate(
        {
            "LANCE_S3_ACCESS_KEY_ID": "k",
            "LANCE_S3_SECRET_ACCESS_KEY": "s",
            "LANCE_CONTROL_EMIT_ENABLED": "true",
            "RASK_SIGNATURE_DOORS": "enforce",
            "RASK_EVENT_SIGNERS": [CATALOG, MAINTENANCE],
            "RASK_EVENT_DELEGATORS": [CATALOG],
            "RASK_CONTROL_SIGNER_ROLES": {"catalog": [CATALOG], "maintenance": [MAINTENANCE]},
        }
    )
    from catalog.api.dapr import register_control_dapr
    from catalog.api.v1.endpoints import events

    app = FastAPI()
    register_control_dapr(app)
    app.include_router(events.router)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.control_buffer = ControlEventBuffer(maxsize=16)
    cache: dict[str, Any] = {
        "acme": {"warehouse_id": "wh-acme", "root_uri": "s3://acme-bucket"},
        "beta": {"warehouse_id": "wh-beta", "root_uri": "s3://beta-bucket"},
    }
    app.state.warehouse_binding_cache = cache
    try:
        with respx.mock(assert_all_called=False) as sidecar:
            for signer in (catalog, maintenance):
                sidecar.get(f"{SECRETS}/{signing_public_secret(signer.identity)}").mock(return_value=httpx.Response(200, json={"keys": signer.public}))
            with TestClient(app) as client:
                yield client, cache, catalog, maintenance
    finally:
        get_settings.cache_clear()


def _deliver(client: TestClient, event: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/control-events", json={"data": event}, headers={"dapr-api-token": APP_TOKEN})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "forge",
    [
        pytest.param(_unsigned, id="unsigned"),
        pytest.param(_signed_by_a_signer_whose_role_is_not_this_actions, id="signed-by-another-role"),
    ],
)
def test_a_control_event_no_signer_of_its_action_signed_reaches_neither_the_feed_nor_the_binding_cache(
    lane: tuple[TestClient, dict[str, Any], Any, Any], forge: Callable[[Any, Any], dict[str, Any]]
) -> None:
    client, cache, catalog, maintenance = lane

    refused = _deliver(client, forge(catalog, maintenance))
    delivered = _deliver(client, catalog.sign_control(GENUINE_DROP, on_behalf_of=GENUINE_DROP["actor"]))

    feed = client.get("/management/v1/events").json()
    assert (refused, delivered) == ({"status": "SUCCESS"}, {"status": "SUCCESS"}), "a refusal is acknowledged, never redelivered"
    assert [event["event_id"] for event in feed["events"]] == ["genuine-drop"], "the change feed holds an event the catalog never signed"
    assert set(cache) == {"acme"}, "the forgery evicted `acme`, or the signed drop did not evict `beta`"
