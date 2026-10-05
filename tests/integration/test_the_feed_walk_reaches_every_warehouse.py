"""The notifications reconciler's walk reaches a run in EVERY warehouse, across both services' real code ([[CTL-021]]).

The walk is the only lane for runs the bus never carries (ingest, Ray TRAIN, external OpenLineage
producers), and it reads lineage's durable feed as its own subject. On the per-dataset-governed `/events`
that subject sees exactly the tables its grants reach, so a run whose output lives in a tenant warehouse it
holds nothing on is filtered out before the walk sees it, while the tick answers 200 and logs
`lineage_feed_reconciled`. `/events/projection` serves the feed unfiltered to a holder of the estate rung
`can_read_event_feed`, and sends each row only the fields targeting reads.

Driven end to end: the reconciler's own feed client over HTTP into lineage's real router, its real
authentication-then-FGA gate, and the real ingress behind the walk. What stands in, and why each may:

* the AGE-backed repository, as a feed of two rows: the feed table is Postgres, which no offline lane runs;
* the verified caller, as lineage's `authenticate` overridden to the principal the reconciler's projected
  `rask-lineage` token maps to (the token's own verification is [[LH-220]]'s, not this claim);
* OpenFGA, as a client answering from the one relation the chart's grant derives for that subject. The
  derivation itself, `event_reader` to `can_read_event_feed` and nothing wider, is `model.fga.yaml`'s claim,
  the one place authz is checked against the real evaluator offline;
* the inbox actor, as a list per subject.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientBatchCheckRequest, ClientCheckRequest, ClientTuple
from openfga_sdk.client.models.batch_check_response import ClientBatchCheckResponse
from openfga_sdk.client.models.batch_check_single_response import ClientBatchCheckSingleResponse
from openfga_sdk.models.check_response import CheckResponse

from lineage.api.security import authenticate
from lineage.api.v1.router import api_router
from lineage.core.config import get_settings
from lineage.schemas import EventRecord
from notifications.api.reconciler import LineageCursor, LineageCursorStore, LineageFeedClient, reconcile
from notifications.api.visibility import Visibility
from notifications.proxies import TypedActorProxy
from service_kit.governed.machine_identity import ServicePrincipal
from service_kit.lakehouse.ns_errors import install_problem_handlers


#: The subject lineage maps the reconciler's service account to (`RASK_SA_SUBJECTS`, from the chart's
#: `services.notifications.env.RASK_LINEAGE_SERVICE_IDENTITY`), and the one the bootstrap hook grants.
RECONCILER = ServicePrincipal(subject="notifications", service_account="system:serviceaccount:rask:rask-sa-notifications")

#: Every relation OpenFGA answers true for, as `(user, relation, object)`: what the bootstrap hook's
#: `event_reader` tuple on the estate root derives for the reconciler, and nothing else.
GRANTED = frozenset({("user:notifications", "can_read_event_feed", "estate:rask")})

#: A run whose output lives in a TENANT warehouse, carrying what an ingest run carries beyond targeting:
#: an input, a column-lineage facet naming another tenant's table, and a job facet.
TENANT_RUN = {
    "eventType": "COMPLETE",
    "eventTime": "2026-10-05T08:00:00+00:00",
    "producer": "https://rask.invalid/ingest",
    "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json",
    "run": {
        "runId": "run-tenant",
        "facets": {
            "author": {"name": "alice", "sub": "alice"},
            "lance": {"operation": "insert", "project": "acme", "run_id": "ingest-7"},
            "errorMessage": {"message": "row 12: value out of range", "programmingLanguage": "python"},
        },
    },
    "job": {"namespace": "ingest", "name": "acme-load", "facets": {"sql": {"query": "SELECT * FROM acme_raw"}}},
    "inputs": [{"namespace": "acme-silver", "name": "acme-silver$pages"}],
    "outputs": [
        {
            "namespace": "acme-gold",
            "name": "acme-gold$catalog",
            "facets": {"columnLineage": {"fields": {"title": {"inputFields": [{"namespace": "globex-gold", "name": "globex-gold$secret", "field": "title"}]}}}},
        }
    ],
}

#: A run whose output lives in the DEFAULT warehouse.
DEFAULT_RUN = {
    "eventType": "FAIL",
    "eventTime": "2026-10-05T07:00:00+00:00",
    "producer": "https://rask.invalid/train",
    "run": {"runId": "run-default", "facets": {"author": {"name": "bob", "sub": "bob"}}},
    "outputs": [{"namespace": "gold", "name": "gold$catalog"}],
}


class _Feed:
    """Lineage's durable feed as the repository serves it: newest first, keyset-paged on `seq < after`."""

    def __init__(self, rows: list[EventRecord]) -> None:
        self._rows = sorted(rows, key=lambda row: row.seq, reverse=True)

    async def list_events(self, limit: int = 500, *, after: int | None = None, summary: bool = False) -> list[EventRecord]:
        rows = [row for row in self._rows if after is None or row.seq < after][:limit]
        return [row.model_copy(update={"event": {}}) for row in rows] if summary else rows

    async def oldest_event_seq(self) -> int | None:
        return min((row.seq for row in self._rows), default=None)


class _OpenFga:
    """Check and BatchCheck answered from `GRANTED`, through the SDK's own request and response classes."""

    async def check(self, body: ClientCheckRequest, options: dict[str, int | str | dict[str, int | str]] | None = None) -> CheckResponse:
        del options
        return CheckResponse(allowed=(body.user, body.relation, body.object) in GRANTED)

    async def batch_check(self, body: ClientBatchCheckRequest, options: dict[str, int | str | dict[str, int | str]] | None = None) -> ClientBatchCheckResponse:
        del options
        return ClientBatchCheckResponse(
            [
                # The SDK hands the batch ITEM back as `request` (client.py `map_response`); its annotation says ClientTuple.
                ClientBatchCheckSingleResponse(
                    allowed=(item.user, item.relation, item.object) in GRANTED, request=cast("ClientTuple", item), correlation_id=f"c{index}", error=None
                )
                for index, item in enumerate(body.checks)
            ]
        )


class _Inbox:
    def __init__(self, boxes: dict[str, list[dict[str, Any]]], subject: str) -> None:
        self._rows = boxes.setdefault(subject, [])

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._rows.append(payload)
        return {"delivered": True, "unread": len(self._rows), "rows": len(self._rows)}


class _Cursor:
    """The cursor store's contract in memory, holding a mark below every row of the feed."""

    def __init__(self) -> None:
        self.cursor = LineageCursor(seq=0, floor=0, updated_at=datetime.now(UTC))

    async def get(self) -> LineageCursor | None:
        return self.cursor

    async def set(self, seq: int, *, resume_from: int | None = None, pending_high: int | None = None, floor: int | None = None, stalls: int = 0) -> None:
        self.cursor = LineageCursor(seq=seq, updated_at=datetime.now(UTC), resume_from=resume_from, pending_high=pending_high, floor=floor, stalls=stalls)


@pytest.fixture
def lineage(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """Lineage's real router behind its real gates, with authentication and FGA on as the chart deploys it."""
    monkeypatch.setenv("RASK_FGA_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ISSUER", "https://idp.invalid/dex")
    monkeypatch.setenv("RASK_OIDC_AUDIENCE", "rask")
    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(api_router)
    app.dependency_overrides[authenticate] = lambda: RECONCILER
    app.state.fga = cast(OpenFgaClient, _OpenFga())
    app.state.repository = _Feed(
        [
            EventRecord(seq=2, outputs=["acme-gold$catalog"], inputs=["acme-silver$pages"], event=TENANT_RUN),
            EventRecord(seq=1, outputs=["gold$catalog"], inputs=[], event=DEFAULT_RUN),
        ]
    )
    yield app
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_the_walk_reaches_a_tenant_warehouse_run_and_is_sent_only_what_targeting_reads(lineage: FastAPI, tmp_path: Path) -> None:
    token = tmp_path / "rask-lineage-token"
    token.write_text("sa-token\n")
    sent: list[dict[str, Any]] = []

    async def record(response: httpx.Response) -> None:
        await response.aread()
        sent.append(response.json())

    boxes: dict[str, list[dict[str, Any]]] = {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=lineage), event_hooks={"response": [record]}) as http:
        feed = LineageFeedClient(client=http, base_url="http://lineage.test", token_file=str(token), timeout_seconds=5.0, page_limit=500)

        result = await reconcile(
            client=feed,
            store=cast(LineageCursorStore, _Cursor()),
            visibility=Visibility(client=None, enabled=False),
            open_inbox=lambda subject: cast(TypedActorProxy, _Inbox(boxes, subject)),
            max_pages=4,
            budget_seconds=10.0,
        )

    assert (result.scanned, result.cursor) == (2, 2)
    assert {subject: [(row["notification_id"], row["object_id"], row["source_run_id"]) for row in rows] for subject, rows in boxes.items()} == {
        "alice": [("run-tenant@COMPLETE", "acme-gold$catalog", "ingest-7")],
        "bob": [("run-default@FAIL", "gold$catalog", None)],
    }
    assert {row["seq"]: row["event"] for page in sent for row in page["events"]}[2] == {
        "eventType": "COMPLETE",
        "eventTime": "2026-10-05T08:00:00+00:00",
        "producer": "https://rask.invalid/ingest",
        "run": {
            "runId": "run-tenant",
            "facets": {"author": {"name": "alice", "sub": "alice"}, "lance": {"operation": "insert", "project": "acme", "run_id": "ingest-7"}},
        },
        "outputs": [{"namespace": "acme-gold", "name": "acme-gold$catalog"}],
    }

    # The rung IS the gate: an unfiltered feed of every warehouse's runs answers a verified caller that holds
    # nothing on the estate root with 403, so a refactor that drops the check fails here rather than shipping.
    lineage.dependency_overrides[authenticate] = lambda: ServicePrincipal(subject="service-ingest", service_account="system:serviceaccount:rask:rask-sa-ingest")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=lineage)) as http:
        stranger = LineageFeedClient(client=http, base_url="http://lineage.test", token_file=str(token), timeout_seconds=5.0, page_limit=500)
        with pytest.raises(httpx.HTTPStatusError) as refused:
            await stranger.page(after=None)
    assert refused.value.response.status_code == 403
