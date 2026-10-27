"""Dapr-native dead-letter wiring (docs/RESILIENCE.md gap #2, opt-in via dapr.resiliency.enabled).

Pins the SUBSCRIPTION CONTRACT the sidecar reads at startup (GET /dapr/subscribe) for lineage's
parking subscription, and what a discarded event records. The parking route ACKs and ERROR-logs: a
dead-lettered message already exhausted a full retry schedule, so re-firing it blind would loop —
visibility, not auto-requeue.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.lakehouse.ns_errors import install_problem_handlers


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


# --------------------------------------------------------------------------------------------------
# A discarded event names WHOSE provenance was lost
# --------------------------------------------------------------------------------------------------


def test_the_verified_author_is_read_off_a_payload_even_when_it_will_not_parse() -> None:
    """Both of lineage's discard paths throw away a payload that CONTAINS the person it belongs to.

    The poison path is the reason this is tolerant rather than a `RunEvent` parse: it fires precisely
    BECAUSE the event failed validation, so the strict model is unavailable exactly where the answer
    is needed.

    `sub` and nothing else, deliberately — the same rule `notifications.author_subject` applies. This
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
