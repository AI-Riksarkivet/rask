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
  drop the dependency and every direct-call test still passes while the graph becomes forgeable.
* **redelivery**. Dapr is at-least-once and the handler answers ``RETRY``, so duplicates are the
  NORMAL case, and the whole design rests on "the graph is idempotent (nodes/edges MERGE)". That
  claim was only checked against real AGE in the e2e tier (``test_lineage_e2e.py``), i.e. never in CI.
* the **DLQ parking route** for lineage. Its subscription *declaration* was pinned; its behaviour
  (ERROR-log + ACK + the terminal-loss counter + the 403) was only pinned for medallion's twin.

Live shape this file mirrors (kind cluster ``lance``, 2026-07-26)::

    kubectl exec lance-ns-lineage-... -c lineage -- ... GET /dapr/subscribe
    [{"pubsubname": "lineage-pubsub-lineage", "topic": "lineage.events.v1",
      "route": "/lineage-events", "deadLetterTopic": "dlq.lineage.events"},
     {"pubsubname": "lineage-pubsub-lineage", "topic": "dlq.lineage.events",
      "route": "/lineage-dlq"}]

Infra-free: no sidecar, no NATS, no database.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lineage.models import RunEvent

from service_kit.lakehouse.ns_errors import install_problem_handlers


_VALID = {
    "eventType": "COMPLETE",
    "eventTime": "2026-06-20T09:00:00Z",
    "run": {"runId": "r1", "facets": {"author": {"name": "alice"}}},
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
    """Stands in for the AGE-backed repository at the ROUTE tier (the graph itself is exercised below)."""

    def __init__(self) -> None:
        self.ingested: list[RunEvent] = []

    async def ingest_event(self, event: RunEvent) -> None:
        self.ingested.append(event)

    async def record_event(self, **_kwargs: object) -> None:
        return None


def _delivery_app(monkeypatch: pytest.MonkeyPatch, *, dlq_topic: str | None = None) -> tuple[TestClient, _FakeRepo]:
    """A real ``register_dapr``-wired app: the production wiring, minus the sidecar and the database."""
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
    app.state.repository = repo
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
    on one side alone loses every event silently — no error, no metric, just an empty graph."""
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


def test_sidecar_delivery_reaches_the_repository_through_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sidecar-shaped POST to the REGISTERED route ingests and ACKs — the seam the handler-only tests
    skip (route registration + the ``Depends`` chain + ``request.app.state.repository`` lookup)."""
    client, repo = _delivery_app(monkeypatch)
    response = client.post("/lineage-events", json=_CLOUD_EVENT, headers={"dapr-api-token": "s3cret"})
    assert response.status_code == 200
    assert response.json() == {"status": "SUCCESS"}
    assert [e.run.run_id for e in repo.ingested] == ["r1"]


def test_forged_delivery_without_the_app_token_is_403_before_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTRACT (the security audit's prod-blocker): the delivery route is co-mounted on the public API
    app, so an unauthenticated POST would let anyone inject fabricated nodes into the authoritative
    graph — self-asserting any ``author`` — EVEN WITH OIDC/FGA on. Only the route tier can prove the
    guard is attached: ``handle_cloud_event`` has no idea the dependency exists."""
    client, repo = _delivery_app(monkeypatch)
    forged = client.post("/lineage-events", json=_CLOUD_EVENT)
    assert forged.status_code == 403
    wrong = client.post("/lineage-events", json=_CLOUD_EVENT, headers={"dapr-api-token": "nope"})
    assert wrong.status_code == 403
    assert repo.ingested == []  # nothing reached the graph


# --------------------------------------------------------------------------- #
# Redelivery — the NORMAL case for at-least-once delivery
# --------------------------------------------------------------------------- #


def test_redelivery_acks_and_writes_the_identical_idempotent_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTRACT: a redelivered event converges instead of duplicating.

    The handler answers ``RETRY`` on a transient failure, so Dapr redelivery is DESIGNED-FOR, not
    exceptional — and the docstring's justification is "the authoritative graph is idempotent
    (nodes/edges MERGE on ``run_id``)". That claim was only ever checked against real AGE in the e2e
    tier, so CI could not catch a ``CREATE`` slipping into the ingest transaction (which would fork a
    duplicate vertex on every redelivery, corrupting the graph).

    Driven through the REAL :class:`LineageRepository` over a capturing ``run_cypher`` (the harness
    ``test_lineage.py`` uses), twice, from the Dapr entry point:

    1. both deliveries ACK ``SUCCESS`` — a duplicate is never mistaken for a failure and re-queued;
    2. no graph write is a bare ``CREATE`` — every vertex/edge write is a ``MERGE`` (or a
       ``MATCH ... SET`` behind one), so replaying converges;
    3. the second delivery emits a statement sequence IDENTICAL to the first — the write set is a pure
       function of the event, which is what makes (2) sufficient.
    """
    import lineage.services.repository as repo_mod
    from lineage.services.consumer import handle_cloud_event

    calls: list[tuple[str, dict[str, object]]] = []

    async def _capture(_conn: object, _graph: str, query: str, params: dict[str, object], *, columns: int = 1) -> list[list[object]]:
        del columns  # signature parity with the real helper
        calls.append((query, params))
        return []  # governance reads see "no node yet"

    class _Conn:
        def transaction(self) -> Any:
            return _Ctx()

    class _Ctx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_a: object) -> bool:
            return False

    class _PoolCM:
        async def __aenter__(self) -> _Conn:
            return _Conn()

        async def __aexit__(self, *_a: object) -> bool:
            return False

    class _Pool:
        def connection(self) -> _PoolCM:
            return _PoolCM()

    monkeypatch.setattr(repo_mod, "run_cypher", _capture)
    repo = repo_mod.LineageRepository(cast(Any, _Pool()), "g")

    first_status = asyncio.run(handle_cloud_event(repo, _CLOUD_EVENT))
    first = list(calls)
    calls.clear()
    second_status = asyncio.run(handle_cloud_event(repo, _CLOUD_EVENT))
    second = list(calls)

    assert first_status == second_status == {"status": "SUCCESS"}
    assert first, "the ingest issued no Cypher — the capture harness drifted"
    creates = sorted({q for q, _ in first if "CREATE (" in q})
    assert not creates, f"ingest must MERGE, never CREATE — a redelivery would duplicate: {creates}"
    assert second == first, "redelivery is not a pure function of the event — convergence is unproven"


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
    the graph is missing events; without it the loss is a log line that scrolls away."""
    import lineage.api.dapr as dapr_mod
    from lineage.core.metrics import Outcome

    client, _ = _delivery_app(monkeypatch, dlq_topic="dlq.lineage.events")
    recorded: list[Outcome] = []
    monkeypatch.setattr(dapr_mod, "record_outcome", recorded.append)

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
    assert recorded == [Outcome.DEAD_LETTERED]


def test_dlq_route_rejects_forged_deliveries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A forged POST must not be able to fake (or hide) a parked message — same token guard."""
    client, _ = _delivery_app(monkeypatch, dlq_topic="dlq.lineage.events")
    assert client.post("/lineage-dlq", json={"id": "x"}).status_code == 403
