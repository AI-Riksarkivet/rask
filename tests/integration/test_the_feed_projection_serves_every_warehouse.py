"""Lineage's feed projection serves an event reader the runs of EVERY warehouse, cut to what targeting reads ([[CTL-021]]).

A client that tells people about the lakehouse's runs reads lineage's durable feed as its own subject, and a run the bus
never carries (an external OpenLineage producer's) reaches it only there. On the per-dataset-governed `/events` that
subject sees exactly the tables its grants reach, so a run whose output lives in a tenant warehouse it holds nothing on
is filtered out. `/events/projection` serves the feed unfiltered to a holder of the estate rung `can_read_event_feed`,
which `event_reader` derives and the estate admin grants a client's own identity at the tuple door ([[LH-563]]), and
sends each row only the fields targeting reads. A caller holding nothing on the estate root is refused.

Driven through lineage's real router behind its real authentication-then-FGA gate. What stands in, and why each may:

* the provenance store, as a feed of two rows: what the feed holds is not this claim, who may read all of it is;
* the verified caller, as lineage's `authenticate` overridden to the principal a client's projected token maps to (the
  token's own verification is [[LH-220]]'s, not this claim);
* OpenFGA, as a client answering from the one relation the chart's grant derives for that subject. The derivation
  itself, `event_reader` to `can_read_event_feed` and nothing wider, is `model.fga.yaml`'s claim, the one place authz is
  checked against the real evaluator offline.

The projection serves each row as lineage recorded it, and an outside producer's run is recorded under the uuid lineage
resolved when it took the event in, never one its body claims ([[LH-522]]): driven through a live catalog and a live
lineage, with authentication off.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientBatchCheckRequest, ClientCheckRequest, ClientTuple
from openfga_sdk.client.models.batch_check_response import ClientBatchCheckResponse
from openfga_sdk.client.models.batch_check_single_response import ClientBatchCheckSingleResponse
from openfga_sdk.models.check_response import CheckResponse

from lakehouse.governed.decider import OpenFgaDecider
from lakehouse.governed.machine_identity import ServicePrincipal
from lakehouse.ns_errors import install_problem_handlers
from lineage.api.security import authenticate
from lineage.api.v1.router import api_router
from lineage.core.config import get_settings
from lineage.schemas import EventRecord
from tests.integration.lineage_app import live_catalog, live_lineage


#: A client's service account holding `event_reader`, granted to its subject
#: (`kubernetes~system:serviceaccount:rask:rask-sa-a-notifier`) at the estate admin's tuple door.
READER = ServicePrincipal.for_account("system:serviceaccount:rask:rask-sa-a-notifier")

#: Every relation OpenFGA answers true for, as `(user, relation, object)`: what the `event_reader` tuple on the estate
#: root derives for the reader, and nothing else.
GRANTED = frozenset({("user:kubernetes~system%3Aserviceaccount%3Arask%3Arask-sa-a-notifier", "can_read_event_feed", "estate:rask")})

#: The uuids the catalog minted for the two runs' outputs, which every governed dataset's `rask_lance` facet carries (ADR 0173
#: §10).
TENANT_UUID, DEFAULT_UUID = "1f2e3d4c-5b6a-4798-8a7b-6c5d4e3f2a1b", "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b"

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
            "rask_author": {"name": "dex~alice", "sub": "dex~alice"},
            "rask_lance": {"operation": "insert", "project": "acme", "runId": "ingest-7"},
            "errorMessage": {"message": "row 12: value out of range", "programmingLanguage": "python"},
        },
    },
    "job": {"namespace": "ingest", "name": "acme-load", "facets": {"sql": {"query": "SELECT * FROM acme_raw"}}},
    "inputs": [{"namespace": "acme-silver", "name": "acme-silver$pages"}],
    "outputs": [
        {
            "namespace": "acme-gold",
            "name": "acme-gold$catalog",
            "facets": {
                "rask_lance": {"tableUuid": TENANT_UUID},
                "columnLineage": {"fields": {"title": {"inputFields": [{"namespace": "globex-gold", "name": "globex-gold$secret", "field": "title"}]}}},
            },
        }
    ],
}

#: A run whose output lives in the DEFAULT warehouse, naming nothing beyond what targeting reads.
DEFAULT_RUN = {
    "eventType": "FAIL",
    "eventTime": "2026-10-05T07:00:00+00:00",
    "producer": "https://rask.invalid/pipeline",
    "run": {"runId": "run-default", "facets": {"rask_author": {"name": "dex~bob", "sub": "dex~bob"}}},
    "outputs": [{"namespace": "gold", "name": "gold$catalog", "facets": {"rask_lance": {"tableUuid": DEFAULT_UUID}}}],
}


class _Feed:
    """Lineage's durable feed as the store serves it: oldest first after `since`, else newest first."""

    def __init__(self, rows: list[EventRecord]) -> None:
        self._rows = sorted(rows, key=lambda row: row.seq)

    async def list_events(self, limit: int = 500, *, after: int | None = None, since: int | None = None, summary: bool = False) -> list[EventRecord]:
        if since is not None:
            rows = [row for row in self._rows if row.seq > since][:limit]
        else:
            rows = [row for row in reversed(self._rows) if after is None or row.seq < after][:limit]
        return [row.model_copy(update={"event": {}}) for row in rows] if summary else rows

    async def feed_bounds(self) -> tuple[int, int] | None:
        return (self._rows[0].seq, self._rows[-1].seq) if self._rows else None


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


@pytest.fixture
def lineage(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """Lineage's real router behind its real gates, with authentication and FGA on as the chart deploys it, answering as `READER`."""
    monkeypatch.setenv("RASK_FGA_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ISSUER", "https://idp.invalid/dex")
    monkeypatch.setenv("RASK_OIDC_AUDIENCE", "rask")
    monkeypatch.setenv("RASK_OIDC_CONNECTORS", '{"local": "dex"}')
    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(api_router)
    app.dependency_overrides[authenticate] = lambda: READER
    app.state.decider = OpenFgaDecider(cast(OpenFgaClient, _OpenFga()))
    app.state.provenance = _Feed(
        [
            EventRecord(seq=2, outputs=["acme-gold$catalog"], inputs=["acme-silver$pages"], event=TENANT_RUN),
            EventRecord(seq=1, outputs=["gold$catalog"], inputs=[], event=DEFAULT_RUN),
        ]
    )
    yield app
    get_settings.cache_clear()


def test_an_event_reader_is_served_every_warehouses_runs_cut_to_what_targeting_reads(lineage: FastAPI) -> None:
    """Both rows reach the reader, the tenant warehouse's included, though it holds no grant on either table. The
    tenant run's inputs, job, schema URL, error message and column lineage stay in the feed: a row keeps the state, the
    time, the producer, the run id with its `rask_author` and `rask_lance` facets, and each output's name with its
    `rask_lance` facet. The default run names nothing more, so it is served whole."""
    with TestClient(lineage) as client:
        served = client.get("/events/projection", params={"since": 0})

    assert served.status_code == 200, served.text
    assert {row["seq"]: row["event"] for row in served.json()["events"]} == {
        1: DEFAULT_RUN,
        2: {
            "eventType": "COMPLETE",
            "eventTime": "2026-10-05T08:00:00+00:00",
            "producer": "https://rask.invalid/ingest",
            "run": {
                "runId": "run-tenant",
                "facets": {
                    "rask_author": {"name": "dex~alice", "sub": "dex~alice"},
                    "rask_lance": {"operation": "insert", "project": "acme", "runId": "ingest-7"},
                },
            },
            "outputs": [{"namespace": "acme-gold", "name": "acme-gold$catalog", "facets": {"rask_lance": {"tableUuid": TENANT_UUID}}}],
        },
    }


def test_a_caller_holding_nothing_on_the_estate_root_is_refused_the_projection(lineage: FastAPI) -> None:
    """The rung is the gate: a verified caller holding nothing on the estate root is refused every warehouse's runs."""
    lineage.dependency_overrides[authenticate] = lambda: ServicePrincipal.for_account("system:serviceaccount:rask:rask-sa-pipeline")
    with TestClient(lineage) as client:
        refused = client.get("/events/projection", params={"since": 0})

    assert (refused.status_code, refused.headers["content-type"], refused.json().get("code")) == (403, "application/problem+json", 15)


def test_an_outside_producers_run_is_recorded_by_its_tables_uuid_not_the_one_it_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    forged = "00000000-0000-4000-8000-000000000000"
    event = {
        **DEFAULT_RUN,
        "eventType": "COMPLETE",
        "job": {"namespace": "pipeline", "name": "nightly"},
        "outputs": [{"namespace": "gold", "name": "gold$catalog", "facets": {"rask_lance": {"tableUuid": forged, "namespaceUuid": forged}}}],
    }
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url, live_lineage(catalog_url, monkeypatch) as lineage_url:
        with httpx.Client(base_url=catalog_url, timeout=60) as catalog:
            assert catalog.post("/v1/namespace/gold/create", json={}).status_code == 200
            assert catalog.post("/v1/table/gold$catalog/declare", json={}).status_code == 200
            table_uuid = catalog.post("/v1/table/gold$catalog/describe", json={}).json()["properties"]["rask.uuid"]
            namespace_uuid = catalog.post("/v1/namespace/gold/describe", json={}).json()["properties"]["rask.uuid"]
        with httpx.Client(base_url=lineage_url, timeout=60) as lineage:
            assert lineage.post("/api/v1/lineage", json=event).status_code == 201
            recorded = lineage.get("/events", params={"table": "gold$catalog"}).json()["events"]

    assert [(row["table_uuids"], row["namespace_uuids"]) for row in recorded] == [([table_uuid], [namespace_uuid])], recorded
