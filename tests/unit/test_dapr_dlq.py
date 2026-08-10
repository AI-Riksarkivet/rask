"""Dapr-native dead-letter wiring (docs/RESILIENCE.md gap #2, opt-in via dapr.resiliency.enabled).

Pins the SUBSCRIPTION CONTRACT the sidecar reads at startup (GET /dapr/subscribe): with a DLQ topic
configured, every subscription declares ``deadLetterTopic`` and the parking route exists; with the
default empty topic the payload is byte-for-byte the pre-existing shape (no silent behavior change —
the chart only sets the env when the Resiliency CRD ships alongside). The parking route itself must
ACK (SUCCESS) and ERROR-log: a dead-lettered message already exhausted a full retry schedule, so
re-firing it blind would loop the cascade — visibility, not auto-requeue.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _mover_app(monkeypatch: pytest.MonkeyPatch, dlq_topic: str | None) -> TestClient:
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    if dlq_topic is None:
        monkeypatch.delenv("MEDALLION_DLQ_TOPIC", raising=False)
    else:
        monkeypatch.setenv("MEDALLION_DLQ_TOPIC", dlq_topic)

    from medallion.api.events import register_stage_route
    from medallion.core.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    register_stage_route(app)
    get_settings.cache_clear()
    return TestClient(app)


def _subs(client: TestClient) -> list[dict]:
    response = client.get("/dapr/subscribe")
    assert response.status_code == 200
    return response.json()


def test_mover_default_has_no_dlq_declaration(monkeypatch: pytest.MonkeyPatch) -> None:
    subs = _subs(_mover_app(monkeypatch, None))
    assert [s["topic"] for s in subs] == ["medallion.bronze"]  # only the stage subscription
    assert not subs[0].get("deadLetterTopic")  # pre-existing shape — no silent behavior change


def test_mover_dlq_declares_dead_letter_and_parking_route(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _mover_app(monkeypatch, "dlq.medallion.bronze")
    subs = {s["topic"]: s for s in _subs(client)}
    assert subs["medallion.bronze"]["deadLetterTopic"] == "dlq.medallion.bronze"
    assert subs["dlq.medallion.bronze"]["route"].endswith("/dlq-event")  # the parking subscription


def test_dlq_route_parks_with_error_log_and_acks(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # Spy on the metric: a parked delivery must be COUNTED (dashboardable/alertable), not only logged —
    # a permanently-stalled cascade item was log-only before (prod-readiness P1).
    import medallion.api.dlq as dlq_mod

    parked: list[str] = []
    monkeypatch.setattr(dlq_mod, "record_dead_letter", parked.append)
    client = _mover_app(monkeypatch, "dlq.medallion.bronze")
    with caplog.at_level(logging.ERROR, logger="medallion.api.dlq"):
        response = client.post(
            "/dlq-event",
            json={"id": "evt-1", "topic": "medallion.bronze", "data": {"token": "t-123"}},
            headers={"dapr-api-token": "s3cret"},
        )
    assert response.status_code == 200
    assert response.json() == {"status": "SUCCESS"}  # parked + ACKed — never a retry loop
    assert any(r.message == "dapr_dead_letter_parked" for r in caplog.records)
    assert len(parked) == 1 and parked[0], f"parked delivery must increment the DLQ counter, got {parked}"


def test_dlq_route_rejects_forged_deliveries(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same token guard as every sidecar-delivered route — a forged POST can't fake a parked message.
    client = _mover_app(monkeypatch, "dlq.medallion.bronze")
    assert client.post("/dlq-event", json={}).status_code == 403


def test_lineage_subscription_declares_dlq_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    monkeypatch.setenv("LINEAGE_DAPR_ENABLED", "true")
    monkeypatch.setenv("LINEAGE_DLQ_TOPIC", "dlq.lineage.events")

    from lineage.api.dapr import register_dapr
    from lineage.core.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    register_dapr(app)
    get_settings.cache_clear()
    subs = {s["topic"]: s for s in _subs(TestClient(app))}
    assert subs["lineage.events.v1"]["deadLetterTopic"] == "dlq.lineage.events"
    assert subs["dlq.lineage.events"]["route"].endswith("/lineage-dlq")


def test_lineage_parking_rides_its_OWN_durable_component_never_the_replay_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """open_dapr.md §2.10: the main lineage component is deliverPolicy=all + ephemeral BY DESIGN
    (replay rebuilds the graph). The parking subscription riding it re-parked up to 168h of
    already-parked dead letters on every pod restart. The chart provisions a dedicated durable
    `-dlq` component and names it in LINEAGE_DLQ_PUBSUB — this pins that the subscription actually
    uses it, and that the no-chart dev fallback stays the main component."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    monkeypatch.setenv("LINEAGE_DAPR_ENABLED", "true")
    monkeypatch.setenv("LINEAGE_DLQ_TOPIC", "dlq.lineage.events")
    monkeypatch.setenv("LINEAGE_DLQ_PUBSUB", "lineage-pubsub-lineage-dlq")

    from lineage.api.dapr import register_dapr
    from lineage.core.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    register_dapr(app)
    get_settings.cache_clear()
    subs = {s["topic"]: s for s in _subs(TestClient(app))}
    assert subs["dlq.lineage.events"]["pubsubname"] == "lineage-pubsub-lineage-dlq"
    assert subs["lineage.events.v1"]["pubsubname"] != "lineage-pubsub-lineage-dlq", "the MAIN subscription must stay on the replay component"

    monkeypatch.delenv("LINEAGE_DLQ_PUBSUB")
    get_settings.cache_clear()
    app2 = FastAPI()
    register_dapr(app2)
    get_settings.cache_clear()
    subs2 = {s["topic"]: s for s in _subs(TestClient(app2))}
    assert subs2["dlq.lineage.events"]["pubsubname"] == subs2["lineage.events.v1"]["pubsubname"], "unset -> dev fallback to the main component"
