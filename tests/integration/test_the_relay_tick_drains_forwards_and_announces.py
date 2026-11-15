"""The relay tick drains the lineage outbox, forwards the feed to the graph backend and prunes it (ADR 0158).

Driven over the real store in a live catalog (`lineage_app`), with a recording graph backend: the tick runs on the
lineage app's own event loop, as its timer runs it, and its report carries every count, zero or not.

Through a read-only window (`LINEAGE_READ_ONLY`, rendered from the chart's one `readOnlyWindow`) the relay's timer is
held: it claims no window and runs nothing, so the first window after the window ends does the work.

The drain is a door to the feed like the bus and the HTTP ingest, and a refusal there is as final: recorded in the
refusals table and retired. So a refusal it records moves `lineage.events.processed` under the outcome every door
counts that refusal by, at the drain's own door, and the alerts reading the outcome see it ([[LH-573]]).

A staged event the drain records is announced again before its staged copy goes: recording repairs the feed and tells
no subscriber, and a publish that fails must find the object still there for the next tick. It goes out on the
connection lineage's own start opened, under the id its producer's publish carried, so a stream that already holds
the event stores the copy once ([[XC-199]]). Driven through lineage's own app and lifespan, where that connection is
made and handed to the tick, and where the relay's timer is started: with a period set, the tick runs with no call
from outside the process.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import cast, override

import httpx
import pytest
from fastapi.testclient import TestClient

from lakehouse import outbox
from lakehouse.bus import BusUnavailableError
from lakehouse.cloud_events import lineage_envelope
from lakehouse.storage_options import StorageOptions
from lakehouse.timers import Outcome, Timer
from lineage import main
from lineage.api import relay
from lineage.core.config import get_settings
from tests.bus_double import BusDouble, session_holding
from tests.integration.lineage_app import SUBJECT_HEADER, governed_lineage, graph_backend, live_catalog, relay_tick, run_event
from tests.traces import trace_id_of


_TABLE = "lance_catalog$acme-silver$pages"


def _declared(catalog_url: str, table: str) -> str:
    """Declare ``table`` in the catalog at ``catalog_url`` and answer the uuid it minted, the object its grants are held by.

    Lineage's HTTP door resolves each name an event names to that uuid as the caller, and checks the grants on it
    ([[LH-524]]), so the table must exist for any event naming it to be admitted.
    """
    namespace = table.rsplit("$", 1)[0]
    with httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        assert catalog.post(f"/v1/table/{table}/declare", json={}).status_code == 200
        return str(catalog.post(f"/v1/table/{table}/describe", json={}).json()["properties"]["rask.uuid"])


def test_a_tick_forwards_the_feed_and_reports_every_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every count is in the report when it is zero: a count that appears only when it did something makes "nothing
    happened" and "the step never ran" the same observation."""
    run = run_event("1d3c5a07-0000-4000-8000-000000000593", "COMPLETE", "2026-10-15T10:00:00+00:00", outputs=[_TABLE])
    with graph_backend() as (graph, url):
        monkeypatch.setenv("LINEAGE_FORWARD_URL", url)
        with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url:
            granted = {("dex~alice", "can_write_data", f"table:{_declared(catalog_url, _TABLE)}")}
            with governed_lineage(catalog_url, monkeypatch, granted) as lineage:
                assert lineage.post("/api/v1/lineage", json=run, headers={SUBJECT_HEADER: "dex~alice"}).status_code == 201
                tick = relay_tick(lineage)

    assert len(graph.received) == 1
    assert tick.model_dump() == {
        "outbox_drained": 0,
        "outbox_stranded": 0,
        "outbox_refused": 0,
        "outbox_refusals_recorded": 0,
        "pruned_events": 0,
        "parks_retired": 0,
        "forwarded": 1,
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
    monkeypatch.setenv("LINEAGE_OUTBOX_URI", outbox_uri)
    monkeypatch.setenv("LINEAGE_SIGNERS", json.dumps(["service-catalog"]))
    unsigned = run_event("1d3c5a07-0000-4000-8000-000000000573", "COMPLETE", "2026-11-12T10:00:00+00:00", outputs=[_TABLE])
    outbox.stage_envelope(outbox_uri, StorageOptions(), lineage_envelope(unsigned))
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url, governed_lineage(catalog_url, monkeypatch, set()) as lineage:
        tick = relay_tick(lineage)

    assert (tick.outbox_refused, tick.outbox_refusals_recorded, processed()) == (1, 1, {("signature_refused", "outbox"): 1})


@contextmanager
def _lineage_as_its_pod_starts_it(catalog_url: str, outbox_uri: str, bus: BusDouble, monkeypatch: pytest.MonkeyPatch, **env: str) -> Generator[TestClient]:
    """Lineage's own app under its own lifespan, recording into the catalog at ``catalog_url`` and draining the outbox at
    ``outbox_uri``, with ``bus`` where the connection its start opens would be, and ``env`` besides."""
    for name, value in {"LINEAGE_CATALOG_URL": catalog_url, "LINEAGE_OUTBOX_URI": outbox_uri, **env}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(main, "bus_session", session_holding(bus))
    get_settings.cache_clear()
    try:
        with TestClient(main.app) as lineage:
            yield lineage
    finally:
        get_settings.cache_clear()


_RUN = "1d3c5a07-0000-4000-8000-000000000787"
#: What a producer stages the run's COMPLETE under, and publishes it as: the id of its envelope.
_STAGED_AS = f"{_RUN}@COMPLETE"
#: The trace of the request that staged it.
_STAGING_REQUEST = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.mark.parametrize(
    ("failing", "drained", "stranded", "sent", "left"),
    [
        pytest.param(None, 1, 0, [("lineage.events.v1", _STAGED_AS)], [], id="the-bus-takes-it"),
        pytest.param(BusUnavailableError("this process is not connected to the bus"), 0, 1, [], [_STAGED_AS], id="the-bus-is-away"),
    ],
)
def test_a_tick_announces_a_staged_event_on_the_services_own_connection_and_drops_it_only_once_the_bus_took_it(  # noqa: PLR0913 — one case's bus and what the tick must leave
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: Exception | None, drained: int, stranded: int, sent: list[tuple[str, str]], left: list[str]
) -> None:
    outbox_uri = f"file://{tmp_path}/outbox"
    event = run_event(_RUN, "COMPLETE", "2026-11-14T10:00:00+00:00", outputs=[_TABLE])
    # Staged in the trace of the request that made it, which the relay's copy is published in (ADR 0209).
    outbox.stage_envelope(outbox_uri, StorageOptions(), lineage_envelope(event).model_copy(update={"traceparent": _STAGING_REQUEST}))
    staged_at_each_publish: list[int] = []

    class _Bus(BusDouble):
        @override
        async def send(self, topic: str, body: bytes, *, message_id: str, timeout: float) -> None:
            staged_at_each_publish.append(len(list(outbox.list_events(outbox_uri, StorageOptions()))))
            await super().send(topic, body, message_id=message_id, timeout=timeout)

    bus = _Bus(failing=failing)
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url, _lineage_as_its_pod_starts_it(catalog_url, outbox_uri, bus, monkeypatch) as lineage:
        tick = relay_tick(lineage)

    assert (tick.outbox_drained, tick.outbox_stranded) == (drained, stranded), tick
    assert [(taken.topic, taken.message_id) for taken in bus.sent] == sent
    assert [(taken.event, trace_id_of(taken.envelope["traceparent"])) for taken in bus.sent] == [(event, trace_id_of(_STAGING_REQUEST))] * len(sent)
    assert (staged_at_each_publish, [key for key, _ in outbox.list_events(outbox_uri, StorageOptions())]) == ([1], left), (
        "the staged copy went before the bus took the event, or stayed after it had"
    )


def test_the_relay_runs_on_lineages_own_timer_with_no_call_from_outside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a period in git, lineage's start keeps the relay's timer, and a staged event reaches the bus and leaves the
    outbox with nothing calling the tick."""
    outbox_uri = f"file://{tmp_path}/outbox"
    event = run_event(_RUN, "COMPLETE", "2026-11-14T10:00:00+00:00", outputs=[_TABLE])
    outbox.stage_envelope(outbox_uri, StorageOptions(), lineage_envelope(event))
    bus = BusDouble()
    timer_env = {"LINEAGE_RELAY_PERIOD_SECONDS": "0.2", "LINEAGE_TIMER_ROOT": str(tmp_path / "timers")}
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url, _lineage_as_its_pod_starts_it(catalog_url, outbox_uri, bus, monkeypatch, **timer_env):
        deadline = time.monotonic() + 30
        while (not bus.sent or list(outbox.list_events(outbox_uri, StorageOptions()))) and time.monotonic() < deadline:
            time.sleep(0.05)

    assert [(taken.topic, taken.message_id) for taken in bus.sent] == [("lineage.events.v1", _STAGED_AS)]
    assert list(outbox.list_events(outbox_uri, StorageOptions())) == [], "the staged copy outlived the event's delivery"


def test_through_a_read_only_window_the_relays_timer_is_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The window writes nothing: the relay's timer claims no window and runs no tick until the window ends."""
    timer_env = {"LINEAGE_READ_ONLY": "true", "LINEAGE_RELAY_PERIOD_SECONDS": "60", "LINEAGE_TIMER_ROOT": str(tmp_path / "timers")}
    with (
        live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url,
        _lineage_as_its_pod_starts_it(catalog_url, f"file://{tmp_path}/outbox", BusDouble(), monkeypatch, **timer_env) as lineage,
    ):
        timer = cast(Timer, getattr(main.app.state, relay.RELAY_TIMER_STATE))
        assert lineage.portal is not None
        turn = lineage.portal.call(timer.turn)

    assert turn.outcome == Outcome.HELD
