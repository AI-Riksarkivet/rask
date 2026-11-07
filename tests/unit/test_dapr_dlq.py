"""Dapr-native dead-letter wiring (docs/RESILIENCE.md gap #2, opt-in via dapr.resiliency.enabled).

Pins the SUBSCRIPTION CONTRACT the sidecar reads at startup (GET /dapr/subscribe) for lineage's
parking subscription, and what a discarded event records. The parking route ACKs and ERROR-logs: a
dead-lettered message already exhausted a full retry schedule, so re-firing it blind would loop —
visibility, not auto-requeue.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lakehouse.ns_errors import install_problem_handlers
from tests.unit import chart_render


def _subs(client: TestClient) -> list[dict]:
    response = client.get("/dapr/subscribe")
    assert response.status_code == 200
    return response.json()


def test_lineage_parking_rides_its_OWN_durable_component_never_the_replay_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """the main lineage component is deliverPolicy=all + ephemeral BY DESIGN
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
    install_problem_handlers(app, logging.getLogger(__name__))
    register_dapr(app)
    get_settings.cache_clear()
    subs = {s["topic"]: s for s in _subs(TestClient(app))}
    assert subs["dlq.lineage.events"]["pubsubname"] == "lineage-pubsub-lineage-dlq"
    assert subs["lineage.events.v1"]["pubsubname"] != "lineage-pubsub-lineage-dlq", "the MAIN subscription must stay on the replay component"

    monkeypatch.delenv("LINEAGE_DLQ_PUBSUB")
    get_settings.cache_clear()
    app2 = FastAPI()
    install_problem_handlers(app2, logging.getLogger(__name__))
    register_dapr(app2)
    get_settings.cache_clear()
    subs2 = {s["topic"]: s for s in _subs(TestClient(app2))}
    assert subs2["dlq.lineage.events"]["pubsubname"] == subs2["lineage.events.v1"]["pubsubname"], "unset -> dev fallback to the main component"


def _catalog_control_lane(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from catalog.api.dapr import register_control_dapr
    from catalog.core.config import get_settings

    for key in ("LANCE_CONTROL_EMIT_ENABLED", "LANCE_CONTROL_PUBSUB", "LANCE_CONTROL_DLQ_TOPIC"):
        if key in env:
            monkeypatch.setenv(key, env[key])
        else:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "s")
    get_settings.cache_clear()
    app = FastAPI()
    register_control_dapr(app)
    get_settings.cache_clear()
    return app


def _maintenance_arrival_lane(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from maintenance.api.arrival import register_arrival_route
    from maintenance.core.config import MaintenanceSettings

    keys = ("MAINTENANCE_ARRIVAL_ENABLED", "MAINTENANCE_LINEAGE_PUBSUB", "MAINTENANCE_LINEAGE_TOPIC", "MAINTENANCE_ARRIVAL_DLQ_TOPIC")
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    app = FastAPI()
    register_arrival_route(app, MaintenanceSettings.model_validate({**{key: env[key] for key in keys if key in env}, "MAINTENANCE_S3_ACCESS_KEY_ID": "test"}))
    return app


@pytest.mark.parametrize("resiliency", ["true", "false"], ids=["retried", "broker-only"])
@pytest.mark.parametrize(
    ("deployment", "route", "dlq_env", "build"),
    [
        pytest.param("Deployment/rask-catalog", "/control-events", "LANCE_CONTROL_DLQ_TOPIC", _catalog_control_lane, id="catalog-control"),
        pytest.param(
            "Deployment/rask-maintenance", "/maintenance-arrival", "MAINTENANCE_ARRIVAL_DLQ_TOPIC", _maintenance_arrival_lane, id="maintenance-arrival"
        ),
    ],
)
def test_a_lakehouse_lane_parks_what_its_retries_cannot_deliver_on_the_topic_the_chart_names(
    deployment: str,
    route: str,
    dlq_env: str,
    build: Callable[[dict[str, str], pytest.MonkeyPatch], FastAPI],
    resiliency: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[[LH-559]] A delivery a lane's retries cannot get taken leaves the subscription on the lane's own dead-letter topic.

    The app is built from the env the chart renders on its Deployment, and the subscription it advertises names that
    topic exactly when its component is one the chart's Resiliency retries: a dead-letter topic with no retry policy
    parks on the first failure, and with the retries on, a subscription with no dead-letter topic leaves a poison event
    to the broker's redelivery. The sidecar's own count of each park (`dapr_component_pubsub_ingress_count{status="drop"}`)
    is what `CatalogControlLaneDeadLettering` and `MaintenanceArrivalDeadLettering` page on, proved by promtool in
    deploy/alerting/rules_test.yml.
    """
    docs = chart_render.render(*chart_render.DEFAULT_ARGS, "--set", f"dapr.resiliency.enabled={resiliency}")
    env = next(chart_render.env_of(c) for workload, _, c in chart_render.containers(docs) if workload == deployment and chart_render.uvicorn_target(c))
    retried = {name for d in docs if d.get("kind") == "Resiliency" for name in d["spec"]["targets"].get("components", {})}

    subscription = next(s for s in _subs(TestClient(build(env, monkeypatch))) if s["route"] == route)

    parked = subscription.get("deadLetterTopic")
    assert parked == env.get(dlq_env), "the subscription must park on the topic the chart renders for it"
    assert (parked is not None) == (subscription["pubsubname"] in retried) == (resiliency == "true"), (
        "a lane names a dead-letter topic exactly when the chart's Resiliency retries its component, which is when the retries are on"
    )


# --------------------------------------------------------------------------------------------------
# A discarded event names WHOSE provenance was lost
# --------------------------------------------------------------------------------------------------


def test_the_verified_author_is_read_off_a_payload_even_when_it_will_not_parse() -> None:
    """Both of lineage's discard paths throw away a payload that CONTAINS the person it belongs to.

    The poison path is the reason this is tolerant rather than a `RunEvent` parse: it fires precisely
    BECAUSE the event failed validation, so the strict model is unavailable exactly where the answer
    is needed.

    `sub` and nothing else, deliberately — the same rule a notifier targets by. This
    model's `author` property prefers `name` and falls back to the standard `ownership` facet, which
    is right for ATTRIBUTION on a board and wrong here: those are producer-supplied and unverified, so
    a loss log built on them could name someone who is not the author of the run that was lost.
    """
    from lineage.models import author_sub_from_payload

    assert author_sub_from_payload({"run": {"facets": {"rask_author": {"name": "Alice A", "sub": "alice"}}}}) == "alice"
    # A name without a verified sub names nobody: the loss is anonymous rather than misattributed.
    assert author_sub_from_payload({"run": {"facets": {"rask_author": {"name": "Alice A"}}}}) is None
    assert author_sub_from_payload({"job": {"facets": {"ownership": {"owners": [{"name": "claimed"}]}}}}) is None
    # Shapes a discard path really meets: truncated, wrong-typed, absent.
    for junk in ({}, {"run": {}}, {"run": {"facets": None}}, {"run": {"facets": {"author": "alice"}}}, "not-a-dict", None):
        assert author_sub_from_payload(junk) is None
