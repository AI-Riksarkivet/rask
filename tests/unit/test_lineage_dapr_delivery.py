"""Seam 1 (DAPR): the lineage ingest as the SIDECAR actually drives it, not the handler in isolation.

``tests/unit/test_consumer.py`` calls :func:`handle_cloud_event` directly, so it pins the ack contract
but never sees the route. Everything between the sidecar and that function was unproven:

* the **subscription contract** the sidecar reads at startup (GET ``/dapr/subscribe``) — pubsub name,
  topic and route. A renamed route or topic silently stops ALL durable ingest (the sidecar 404s /
  subscribes to a topic nobody publishes); nothing else in the suite would notice. Only the
  *DLQ-configured* variant was pinned (``test_dapr_dlq.py``), never the default one the chart ships
  before ``dapr.resiliency.enabled``.
* the **token guard on the delivery route**. ``on_lineage_event`` declares
  ``Depends(require_dapr_token)``, which a direct call to ``handle_cloud_event`` cannot exercise —
  drop the dependency and every direct-call test still passes while the feed becomes forgeable.
* the **DLQ parking route** for lineage. Its subscription *declaration* was pinned; its behaviour
  (ERROR-log + ACK + the terminal-loss counter + the 403) was only pinned for medallion's twin.

Live shape this file mirrors (kind cluster ``lance``, 2026-07-26)::

    kubectl exec lance-ns-lineage-... -c lineage -- ... GET /dapr/subscribe
    [{"pubsubname": "lineage-pubsub-lineage", "topic": "lineage.events.v1",
      "route": "/lineage-events", "deadLetterTopic": "dlq.lineage.events"},
     {"pubsubname": "lineage-pubsub-lineage", "topic": "dlq.lineage.events",
      "route": "/lineage-dlq"}]

Infra-free: no sidecar, no NATS, no catalog. That a redelivery lands nothing twice is the store's claim,
pinned through the real catalog by ``tests/integration/test_the_commit_feed_is_a_lance_table_followed_forward.py``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lineage.models import RunEvent
from service_kit.lakehouse.ns_errors import install_problem_handlers


_VALID = {
    "eventType": "COMPLETE",
    "eventTime": "2026-06-20T09:00:00Z",
    "run": {"runId": "r1", "facets": {"rask_author": {"name": "alice"}}},
    "job": {"namespace": "ray-jobs", "name": "ingest"},
    "inputs": [{"namespace": "source", "name": "raw"}],
    "outputs": [
        {
            "namespace": "bronze",
            "name": "bronze$x",
            "facets": {
                "version": {"datasetVersion": "3"},
                "columnLineage": {"fields": {"id": {"inputFields": [{"namespace": "source", "name": "raw", "field": "id"}]}}},
            },
        }
    ],
}
#: Dapr wraps the published data in a CloudEvent envelope; the OpenLineage event rides in ``data``.
_CLOUD_EVENT = {"id": "ce-1", "source": "catalog", "type": "com.dapr.event.sent", "data": _VALID}


class _FakeRepo:
    """Stands in for the provenance store at the ROUTE tier."""

    def __init__(self) -> None:
        self.ingested: list[RunEvent] = []
        self.refusals: list[dict[str, str | None]] = []

    async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
        """[[LH-182]] The drain now RECORDS a settled refusal before retiring the object, so a double
        that cannot record one no longer stands in for the store. Captured rather than ignored:
        several of these tests assert what the refusal path did, and a silent no-op would let a drain
        that recorded nothing pass as one that did."""
        self.refusals.append({"outbox_key": outbox_key, "run_id": run_id, "author": author, "reason": reason, "event_json": event_json})

    async def record(self, event: RunEvent) -> None:
        # The edges AND the feed row — one call, nothing to stub twice.
        self.ingested.append(event)


def _delivery_app(monkeypatch: pytest.MonkeyPatch, *, dlq_topic: str | None = None) -> tuple[TestClient, _FakeRepo]:
    """A real ``register_dapr``-wired app: the production wiring, minus the sidecar and the catalog."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    monkeypatch.setenv("LINEAGE_DAPR_ENABLED", "true")
    if dlq_topic is None:
        monkeypatch.delenv("LINEAGE_DLQ_TOPIC", raising=False)
    else:
        monkeypatch.setenv("LINEAGE_DLQ_TOPIC", dlq_topic)

    from lineage.api.dapr import register_dapr
    from lineage.core.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    register_dapr(app)
    get_settings.cache_clear()
    repo = _FakeRepo()
    app.state.provenance = repo
    return TestClient(app), repo


def _subs(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/dapr/subscribe")
    assert response.status_code == 200
    return {s["topic"]: s for s in response.json()}


# --------------------------------------------------------------------------- #
# The subscription contract the sidecar reads at startup
# --------------------------------------------------------------------------- #


def test_default_subscription_contract_is_the_one_the_publisher_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTRACT: pubsub ``lineage-pubsub`` / topic ``lineage.events.v1`` / route ``/lineage-events``.

    This is the whole durable ingest path in three strings, and it is a CROSS-PROCESS contract: the
    catalog publishes to that (component, topic) and the sidecar POSTs that route. Renaming any of them
    on one side alone loses every event silently — no error, no metric, just an empty feed."""
    client, _ = _delivery_app(monkeypatch)
    subs = _subs(client)
    assert set(subs) == {"lineage.events.v1"}  # exactly one subscription without a DLQ configured
    sub = subs["lineage.events.v1"]
    assert sub["pubsubname"] == "lineage-pubsub"
    assert sub["route"].endswith("/lineage-events")
    assert not sub.get("deadLetterTopic")  # pre-resiliency shape — no silent behaviour change


def test_dapr_disabled_registers_no_ingest_route_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTRACT: an HTTP-only deployment carries NO always-live ingest route (``register_dapr`` mounts
    GET /dapr/subscribe unconditionally but the delivery route only when Dapr ingest is on)."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    monkeypatch.delenv("LINEAGE_DAPR_ENABLED", raising=False)

    from lineage.api.dapr import register_dapr
    from lineage.core.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    register_dapr(app)
    get_settings.cache_clear()
    client = TestClient(app)
    assert client.get("/dapr/subscribe").json() == []
    assert client.post("/lineage-events", json=_CLOUD_EVENT).status_code == 404


# --------------------------------------------------------------------------- #
# The delivery route itself (what the sidecar POSTs)
# --------------------------------------------------------------------------- #


def test_sidecar_delivery_reaches_the_store_through_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sidecar-shaped POST to the REGISTERED route ingests and ACKs — the seam the handler-only tests
    skip (route registration + the ``Depends`` chain + ``request.app.state.provenance`` lookup)."""
    client, repo = _delivery_app(monkeypatch)
    response = client.post("/lineage-events", json=_CLOUD_EVENT, headers={"dapr-api-token": "s3cret"})
    assert response.status_code == 200
    assert response.json() == {"status": "SUCCESS"}
    assert [e.run.run_id for e in repo.ingested] == ["r1"]


def test_forged_delivery_without_the_app_token_is_403_before_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTRACT (the security audit's prod-blocker): the delivery route is co-mounted on the public API
    app, so an unauthenticated POST would let anyone inject fabricated nodes into the authoritative
    feed — self-asserting any ``author`` — EVEN WITH OIDC/FGA on. Only the route tier can prove the
    guard is attached: ``handle_cloud_event`` has no idea the dependency exists."""
    client, repo = _delivery_app(monkeypatch)
    forged = client.post("/lineage-events", json=_CLOUD_EVENT)
    assert forged.status_code == 403
    wrong = client.post("/lineage-events", json=_CLOUD_EVENT, headers={"dapr-api-token": "nope"})
    assert wrong.status_code == 403
    assert repo.ingested == []  # nothing reached the feed


# --------------------------------------------------------------------------- #
# Redelivery — the NORMAL case for at-least-once delivery
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# The dead-letter parking route
# --------------------------------------------------------------------------- #


def test_dlq_subscription_and_parking_route_are_declared_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _delivery_app(monkeypatch, dlq_topic="dlq.lineage.events")
    subs = _subs(client)
    assert subs["lineage.events.v1"]["deadLetterTopic"] == "dlq.lineage.events"
    assert subs["dlq.lineage.events"]["route"].endswith("/lineage-dlq")
    # The parking subscription must NOT itself dead-letter, or an exhausted delivery loops forever.
    assert not subs["dlq.lineage.events"].get("deadLetterTopic")


def test_dlq_route_parks_with_an_error_log_acks_and_counts_the_loss(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """CONTRACT: a parked delivery is ACKed (never re-fired blind — it already exhausted the schedule),
    ERROR-logged, and COUNTED as ``dead_lettered``. The counter is the only dashboardable signal that
    the feed is missing events; without it the loss is a log line that scrolls away."""
    import lineage.api.dapr as dapr_mod
    from lineage.core.metrics import Door, Outcome

    client, _ = _delivery_app(monkeypatch, dlq_topic="dlq.lineage.events")
    recorded: list[tuple[Outcome, Door]] = []

    def _record(outcome: Outcome, *, door: Door) -> None:
        # The DOOR is asserted alongside the outcome: one counter is written by the subscriber, this
        # dead-letter handler and the HTTP ingest, all from one pod, so the label is the only thing
        # that tells an operator which of them lost the run.
        recorded.append((outcome, door))

    monkeypatch.setattr(dapr_mod, "record_outcome", _record)

    with caplog.at_level(logging.ERROR, logger="lineage.api.dapr"):
        response = client.post(
            "/lineage-dlq",
            json={"id": "ce-9", "topic": "lineage.events.v1", "data": _VALID},
            headers={"dapr-api-token": "s3cret"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "SUCCESS"}
    parked = [r for r in caplog.records if r.message == "dapr_dead_letter_parked"]
    assert parked and getattr(parked[0], "event_id", None) == "ce-9"  # attributable to the lost delivery
    assert recorded == [(Outcome.DEAD_LETTERED, Door.DEAD_LETTER)]


def test_dlq_route_rejects_forged_deliveries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A forged POST must not be able to fake (or hide) a parked message — same token guard."""
    client, _ = _delivery_app(monkeypatch, dlq_topic="dlq.lineage.events")
    assert client.post("/lineage-dlq", json={"id": "x"}).status_code == 403
