"""The relay tick answers only its sidecar's cron binding, at the binding's exact name (ADR 0158).

The tick drains the lineage outbox, prunes the feed and forwards it to the graph backend, so a caller who could drive
it could re-publish whatever is staged and push the feed out on demand. The route is mounted only when the chart names
a binding, answers Dapr's discovery pre-flight without the app token (the probe carries none), and refuses a POST
without it. Driven through lineage's router over the real store in a live catalog (`lineage_app`), with a recording
graph backend, so a refused tick is seen to forward nothing.

Through a read-only window (`LINEAGE_READ_ONLY`, rendered from the chart's one `readOnlyWindow`) the tick is answered and
does nothing: it drains, prunes and forwards nothing, and says it paused, so the next tick after the window does the work.

The drain is a door to the feed like the bus and the HTTP ingest, and a refusal there is as final: recorded in the
refusals table and retired. So a refusal it records moves `lineage.events.processed` under the outcome every door
counts that refusal by, at the drain's own door, and the alerts reading the outcome see it ([[LH-573]]).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lakehouse import outbox
from lakehouse.ns_errors import install_problem_handlers
from lakehouse.storage_options import StorageOptions
from tests.integration.lineage_app import RELAY, SUBJECT_HEADER, governed_lineage, graph_backend, live_catalog, run_event


_TABLE = "acme-silver$pages"


def _declared(catalog_url: str, table: str) -> str:
    """Declare ``table`` in the catalog at ``catalog_url`` and answer the uuid it minted, the object its grants are held by.

    Lineage's HTTP door resolves each name an event names to that uuid as the caller, and checks the grants on it
    ([[LH-524]]), so the table must exist for any event naming it to be admitted.
    """
    namespace = table.split("$")[0]
    with httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        assert catalog.post(f"/v1/table/{table}/declare", json={}).status_code == 200
        return str(catalog.post(f"/v1/table/{table}/describe", json={}).json()["properties"]["rask.uuid"])


def test_the_app_mounts_the_tick_only_when_a_binding_names_it() -> None:
    from lineage.main import mount_relay_cron

    bare, mounted = FastAPI(), FastAPI()
    install_problem_handlers(bare, logging.getLogger(__name__))
    install_problem_handlers(mounted, logging.getLogger(__name__))

    assert (mount_relay_cron(bare, None), mount_relay_cron(bare, ""), mount_relay_cron(mounted, RELAY)) == (False, False, True)
    assert (TestClient(bare).options(f"/{RELAY}").status_code, TestClient(mounted).options(f"/{RELAY}").status_code) == (404, 200)


def test_the_discovery_preflight_is_answered_at_the_binding_name_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url, governed_lineage(catalog_url, monkeypatch, set()) as lineage:
        answers = (lineage.options(f"/{RELAY}"), lineage.options("/other-binding"))

    assert (answers[0].json(), answers[1].status_code) == ({"status": "ok"}, 404)


@pytest.mark.parametrize(("read_only", "forwarded"), [pytest.param(False, 1, id="outside-a-window"), pytest.param(True, 0, id="through-a-read-only-window")])
def test_a_tick_forwards_only_with_the_sidecars_token_and_outside_a_read_only_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_only: bool, forwarded: int
) -> None:
    """Every count is in the report when it is zero: a count that appears only when it did something makes "nothing
    happened" and "the step never ran" the same observation."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    monkeypatch.setenv("LINEAGE_READ_ONLY", str(read_only).lower())
    run = run_event("1d3c5a07-0000-4000-8000-000000000593", "COMPLETE", "2026-10-15T10:00:00+00:00", outputs=[_TABLE])
    with graph_backend() as (graph, url):
        monkeypatch.setenv("LINEAGE_FORWARD_URL", url)
        with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url:
            granted = {("dex~alice", "can_write_data", f"table:{_declared(catalog_url, _TABLE)}")}
            with governed_lineage(catalog_url, monkeypatch, granted) as lineage:
                assert lineage.post("/api/v1/lineage", json=run, headers={SUBJECT_HEADER: "dex~alice"}).status_code == 201
                refused = [lineage.post(f"/{RELAY}").status_code, lineage.post(f"/{RELAY}", headers={"dapr-api-token": "wrong"}).status_code]
                sent_while_refused = len(graph.received)
                tick = lineage.post(f"/{RELAY}", headers={"dapr-api-token": "s3cret"})

    assert (refused, sent_while_refused, len(graph.received)) == ([403, 403], 0, forwarded)
    assert tick.json() == {
        "paused": read_only,
        "outbox_drained": 0,
        "outbox_stranded": 0,
        "outbox_refused": 0,
        "outbox_refusals_recorded": 0,
        "pruned_events": 0,
        "parks_retired": 0,
        "forwarded": forwarded,
        "forward_refused": 0,
        "forward_stalled": None,
    }


@pytest.fixture
def processed(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], dict[tuple[str, str], int]]]:
    """`lineage.events.processed` by outcome and door, read through a real in-memory reader.

    `record_outcome` adds to the counter the metrics module holds when it is called, so that counter is swapped for one of
    the same name under this reader's provider, and every door's count lands here.
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

    from lineage.core import metrics

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(metrics, "_events_processed", provider.get_meter("lance.lineage").create_counter("lineage.events.processed", unit="{event}"))

    def counts() -> dict[tuple[str, str], int]:
        found: dict[tuple[str, str], int] = {}
        data = reader.get_metrics_data()
        for resource in data.resource_metrics if data is not None else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    # A counter only ever yields `NumberDataPoint`, the member of the reader's point union that has a value.
                    for point in cast(Sequence[NumberDataPoint], metric.data.data_points):
                        attributes = point.attributes or {}
                        found[(str(attributes.get("lance.lineage.outcome")), str(attributes.get("lance.lineage.door")))] = int(point.value)
        return found

    yield counts
    provider.shutdown()


def test_a_tick_counts_a_refusal_it_records_as_every_door_counts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, processed: Callable[[], dict[tuple[str, str], int]]
) -> None:
    """A staged event no listed signer signed, refused by the bus door's own gate as the tick drains it."""
    outbox_uri = f"file://{tmp_path}/outbox"
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    monkeypatch.setenv("LINEAGE_OUTBOX_URI", outbox_uri)
    monkeypatch.setenv("LINEAGE_SIGNERS", json.dumps(["service-catalog"]))
    unsigned = run_event("1d3c5a07-0000-4000-8000-000000000573", "COMPLETE", "2026-11-12T10:00:00+00:00", outputs=[_TABLE])
    outbox.stage_event(outbox_uri, StorageOptions(), unsigned["run"]["runId"], json.dumps(unsigned))
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url, governed_lineage(catalog_url, monkeypatch, set()) as lineage:
        tick = lineage.post(f"/{RELAY}", headers={"dapr-api-token": "s3cret"})

    assert (tick.json()["outbox_refused"], tick.json()["outbox_refusals_recorded"], processed()) == (1, 1, {("signature_refused", "outbox"): 1})
