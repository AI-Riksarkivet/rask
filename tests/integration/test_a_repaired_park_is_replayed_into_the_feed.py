"""[[LH-148]] A parked lineage delivery is kept, and replayed into the feed on demand once its cause is fixed.

The stream drops a message past its retention and nothing reads an ended one back, so lineage's ingest lane keeps each
delivery it parks in the provenance namespace's ``refusals`` table, and the estate admin re-presents it through
``POST /admin/dlq/parked/{run_id}/replay``: the same authorization and signature check the bus applies, written to the
feed directly, publishing nothing on ``lineage.events.v1``. Each delivery is judged on its own feed row, since a run's
START reaching the feed says nothing of its COMPLETE, the event naming the outputs a missing grant refuses.

Driven through lineage's router over the real store in a live catalog (`lineage_app`), with lineage's own ingest lane
taking its deliveries from a `tests.bus_double.BusDouble` on the app's loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from anyio.from_thread import BlockingPortal
from fastapi.testclient import TestClient
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientTuple

from lakehouse.bus_delivery import Outcome, Settlement
from lakehouse.governed import fga
from lakehouse.governed.fga import user_object
from tests.bus_double import BusDouble
from tests.integration.lineage_app import RELAY, SUBJECT_HEADER, governed_lineage, live_catalog, run_event


_TABLE = "lance_catalog$acme-silver$parked"
_RUN = "run-parked"
_ADMIN = "dex~root"
_AUTHOR = "dex~alice"
_TOKEN = {"dapr-api-token": "s3cret"}
_REPLAY = f"/admin/dlq/parked/{_RUN}/replay"


def _declared(catalog_url: str, table: str) -> str:
    namespace = table.rsplit("$", 1)[0]
    with httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        assert catalog.post(f"/v1/table/{table}/declare", json={}).status_code == 200
        return str(catalog.post(f"/v1/table/{table}/describe", json={}).json()["properties"]["rask.uuid"])


def _event(state: str, table: str, *, outputs: bool) -> dict[str, Any]:
    """The run's ``state`` event, as the bus delivers it: each output named by the uuid the catalog stamps on it."""
    data = run_event(_RUN, state, "2026-10-27T09:00:00Z", outputs=[_TABLE] if outputs else [], facets={"rask_author": {"sub": _AUTHOR}})
    for output in data["outputs"]:
        output["facets"] = {"rask_lance": {"tableUuid": table.removeprefix("table:")}}
    return data


class _Estate:
    """Lineage on a live catalog, the estate admin granted, the table the run writes, and the bus its lane takes from."""

    def __init__(self, lineage: TestClient, portal: BlockingPortal, table: str, bus: BusDouble) -> None:
        self.lineage = lineage
        self.portal = portal
        self.table = table
        self.bus = bus

    def deliver(self, envelope: dict[str, Any]) -> Settlement:
        """Hand ``envelope`` to lineage's ingest lane as its first delivery; answers what the bus is told."""
        from lineage.api.ingest_lane import CONSUMER

        return self.portal.call(partial(self.bus.deliver, CONSUMER, envelope))


def _governed(monkeypatch: pytest.MonkeyPatch, table: str) -> None:
    """OpenFGA holding its owner's grant on ``table``, as the catalog writes one on a declare: a refusal on it is a
    missing grant a tuple repairs, which the lane parks, rather than a table nothing can be granted on."""

    async def held(client: OpenFgaClient, obj: str, **_retry: float) -> list[ClientTuple]:
        del client
        return [ClientTuple(user=user_object("dex~owner"), relation="owner", object=obj)] if obj == table else []

    monkeypatch.setattr(fga, "read_object_tuples", held)


@contextmanager
def _estate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[_Estate]:
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url:
        table = f"table:{_declared(catalog_url, _TABLE)}"
        _governed(monkeypatch, table)
        from lineage.api.ingest_lane import IngestLane, lane
        from lineage.core.config import get_settings

        with governed_lineage(catalog_url, monkeypatch, set()) as lineage:
            settings = get_settings()
            lineage.app.state.grants.allowed.update(
                (user_object(_ADMIN), verb, settings.fga_root_object) for verb in ("can_read_parked_events", "can_replay_parked_events")
            )
            bus = BusDouble()
            lineage.app.state.bus = bus
            ingest = IngestLane(lineage.app, settings)
            portal = lineage.portal
            assert portal is not None, "the client runs no lifespan, so nothing would take a delivery"
            # The lane as lineage's lifespan starts it (`lineage.main._take_deliveries`), on the app's own loop.
            taking = portal.start_task_soon(partial(bus.consume, lane(settings), ingest.handle, park=ingest.park))
            try:
                yield _Estate(lineage, portal, table, bus)
            finally:
                taking.cancel()


def _recorded(lineage: TestClient, state: str, table: str) -> bool:
    from lineage.services.provenance import delivery_feed_key

    key = delivery_feed_key({"data": _event(state, table, outputs=True)})
    return key in asyncio.run(lineage.app.state.provenance.events_recorded({key}))


def test_a_refused_complete_is_kept_and_replayed_into_the_feed_once_its_tuple_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _estate(tmp_path, monkeypatch) as estate:
        lineage, table = estate.lineage, estate.table
        started = estate.deliver({"id": "evt-start", "data": _event("START", table, outputs=False)})
        parked = estate.deliver({"id": "evt-complete", "data": _event("COMPLETE", table, outputs=True)})
        listed = lineage.get("/admin/dlq/parked", headers={SUBJECT_HEADER: _ADMIN})
        not_admin = lineage.post(_REPLAY, headers={SUBJECT_HEADER: _AUTHOR})
        refused = lineage.post(_REPLAY, headers={SUBJECT_HEADER: _ADMIN})
        lineage.app.state.grants.allowed.add((user_object(_AUTHOR), "can_write_data", table))
        replayed = lineage.post(_REPLAY, headers={SUBJECT_HEADER: _ADMIN})
        again = lineage.post(_REPLAY, headers={SUBJECT_HEADER: _ADMIN})
        left = lineage.get("/admin/dlq/parked", headers={SUBJECT_HEADER: _ADMIN})
        recorded = _recorded(lineage, "COMPLETE", table)

    assert (started, parked) == (Settlement(outcome=Outcome.TAKEN), Settlement(outcome=Outcome.PARKED))
    assert listed.json() == {"runs": {_RUN: 1}}, "a COMPLETE refused after its START landed was not kept"
    assert (not_admin.status_code, "can_replay_parked_events" in not_admin.json()["detail"]) == (403, True), not_admin.text
    assert (refused.status_code, refused.json()["code"]) == (400, 13), refused.text
    assert (replayed.status_code, replayed.json()) == (200, {"status": "recovered", "run_id": _RUN}), replayed.text
    assert (again.status_code, left.json()) == (404, {"runs": {}}), "a recovered park is still kept"
    assert recorded, "the feed lacks the COMPLETE the door recovered"
    assert estate.bus.sent == [], "a replayed park was published again"


def test_the_relay_tick_retires_a_kept_park_the_feed_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A kept delivery whose row reaches the feed by another path (here the producer's own ingest over HTTP) is retired
    by the next relay tick, rather than kept until an admin replays it."""
    with _estate(tmp_path, monkeypatch) as estate:
        lineage, table = estate.lineage, estate.table
        parked = estate.deliver({"id": "evt-complete", "data": _event("COMPLETE", table, outputs=True)})
        lineage.app.state.grants.allowed.add((user_object(_AUTHOR), "can_write_data", table))
        ingested = lineage.post("/api/v1/lineage", json=_event("COMPLETE", table, outputs=True), headers={SUBJECT_HEADER: _AUTHOR})
        tick = lineage.post(f"/{RELAY}", headers=_TOKEN)
        left = lineage.get("/admin/dlq/parked", headers={SUBJECT_HEADER: _ADMIN})

    assert (parked, ingested.status_code) == (Settlement(outcome=Outcome.PARKED), 201), ingested.text
    assert (tick.json()["parks_retired"], left.json()) == (1, {"runs": {}})
