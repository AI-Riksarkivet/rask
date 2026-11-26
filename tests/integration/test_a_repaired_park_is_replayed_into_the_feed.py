"""[[LH-148]] A parked lineage delivery is kept, and replayed into the feed on demand once its cause is fixed.

The stream drops a message past its retention and nothing reads an ended one back, so lineage's ingest lane keeps each
delivery it parks in the provenance namespace's ``refusals`` table, and the estate admin re-presents it through
``POST /admin/dlq/parked/{run_id}/replay``: the same authorization and signature check the bus applies, written to the
feed directly, publishing nothing on ``lineage.events.v1``. Each delivery is judged on its own feed row, since a run's
START reaching the feed says nothing of its COMPLETE, the event naming the outputs a missing grant refuses.

Driven through lineage's router over the real store in a live catalog (`lineage_app`), with lineage's own ingest lane
taking its deliveries from a `tests.bus_double.BusDouble` on the app's loop.
"""

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from anyio.from_thread import BlockingPortal
from fastapi.testclient import TestClient
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientTuple

from lakehouse.events.bus_delivery import Outcome, Settlement
from lakehouse.governed import fga_reads
from lakehouse.governed.fga_ids import user_object
from tests.app_state import app_of
from tests.bus_double import BusDouble
from tests.integration.lineage_app import SUBJECT_HEADER, declared, governed_lineage, live_catalog, relay_tick, run_event


_TABLE = "lance_catalog$acme-silver$parked"
_RUN = "run-parked"
_ADMIN = "dex~root"
_AUTHOR = "dex~alice"
_REPLAY = f"/admin/dlq/parked/{_RUN}/replay"


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

    monkeypatch.setattr(fga_reads, "read_object_tuples", held)


@contextmanager
def _estate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[_Estate]:
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url:
        table = f"table:{declared(catalog_url, _TABLE)[_TABLE]}"
        _governed(monkeypatch, table)
        from lineage.api.ingest_lane import IngestLane, lane
        from lineage.core.config import get_settings

        with governed_lineage(catalog_url, monkeypatch, set()) as lineage:
            settings = get_settings()
            app_of(lineage).state.grants.allowed.update(
                (user_object(_ADMIN), verb, settings.fga_root_object) for verb in ("can_read_parked_events", "can_replay_parked_events")
            )
            bus = BusDouble()
            app_of(lineage).state.bus = bus
            ingest = IngestLane(app_of(lineage), settings)
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
    return key in asyncio.run(app_of(lineage).state.provenance.events_recorded({key}))


def test_a_refused_complete_is_kept_and_replayed_into_the_feed_once_its_tuple_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _estate(tmp_path, monkeypatch) as estate:
        lineage, table = estate.lineage, estate.table
        started = estate.deliver({"id": "evt-start", "data": _event("START", table, outputs=False)})
        parked = estate.deliver({"id": "evt-complete", "data": _event("COMPLETE", table, outputs=True)})
        listed = lineage.get("/admin/dlq/parked", headers={SUBJECT_HEADER: _ADMIN})
        not_admin = lineage.post(_REPLAY, headers={SUBJECT_HEADER: _AUTHOR})
        refused = lineage.post(_REPLAY, headers={SUBJECT_HEADER: _ADMIN})
        app_of(lineage).state.grants.allowed.add((user_object(_AUTHOR), "can_write_data", table))
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


def test_a_staged_event_a_grant_would_admit_is_parked_by_the_relay_and_replayed_into_the_feed_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The outbox drain's refusal has the bus lane's repair: kept where the estate admin lists and replays it, its staged
    copy retired, and recorded once after the grant is written, however often the relay ticks or the admin replays."""
    from lakehouse.events import outbox
    from lakehouse.events.cloud_events import lineage_envelope
    from lakehouse.storage.options import StorageOptions

    outbox_uri = f"file://{tmp_path}/outbox"
    monkeypatch.setenv("LINEAGE_OUTBOX_URI", outbox_uri)
    with _estate(tmp_path, monkeypatch) as estate:
        lineage, table = estate.lineage, estate.table
        outbox.stage_envelope(outbox_uri, StorageOptions(), lineage_envelope(_event("COMPLETE", table, outputs=True)))
        refused = relay_tick(lineage)
        listed = lineage.get("/admin/dlq/parked", headers={SUBJECT_HEADER: _ADMIN})
        staged = list(outbox.list_events(outbox_uri, StorageOptions()))
        app_of(lineage).state.grants.allowed.add((user_object(_AUTHOR), "can_write_data", table))
        replayed = lineage.post(_REPLAY, headers={SUBJECT_HEADER: _ADMIN})
        again = lineage.post(_REPLAY, headers={SUBJECT_HEADER: _ADMIN})
        relay_tick(lineage)
        left = lineage.get("/admin/dlq/parked", headers={SUBJECT_HEADER: _ADMIN})
        feed = asyncio.run(app_of(lineage).state.provenance.list_events())

    assert (refused.outbox_refused, staged, listed.json()) == (1, [], {"runs": {_RUN: 1}}), "the refused staged event is not kept for replay"
    assert (replayed.status_code, replayed.json()) == (200, {"status": "recovered", "run_id": _RUN}), replayed.text
    assert (again.status_code, left.json()) == (404, {"runs": {}}), "a recovered park is still kept"
    assert [record.event_type for record in feed] == ["COMPLETE"], "the feed does not hold the replayed event exactly once"


def test_the_relay_tick_retires_every_kept_park_the_feed_holds_one_read_after_another(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A kept delivery whose row reaches the feed by another path (here the producer's own ingest over HTTP) is retired
    by the next relay tick, rather than kept until an admin replays it; with more parks kept than one read of the
    retirement takes, those of every read are retired."""
    from lineage.services import provenance

    monkeypatch.setattr(provenance, "_PARKS_PER_READ", 1)
    states = ("START", "COMPLETE")
    with _estate(tmp_path, monkeypatch) as estate:
        lineage, table = estate.lineage, estate.table
        parked = [estate.deliver({"id": f"evt-{state}", "data": _event(state, table, outputs=True)}) for state in states]
        kept = lineage.get("/admin/dlq/parked", headers={SUBJECT_HEADER: _ADMIN})
        app_of(lineage).state.grants.allowed.add((user_object(_AUTHOR), "can_write_data", table))
        ingested = [lineage.post("/api/v1/lineage", json=_event(state, table, outputs=True), headers={SUBJECT_HEADER: _AUTHOR}) for state in states]
        tick = relay_tick(lineage)
        left = lineage.get("/admin/dlq/parked", headers={SUBJECT_HEADER: _ADMIN})

    assert (parked, kept.json()) == ([Settlement(outcome=Outcome.PARKED)] * 2, {"runs": {_RUN: 2}})
    assert [answer.status_code for answer in ingested] == [201, 201], [answer.text for answer in ingested]
    assert (tick.parks_retired, left.json()) == (2, {"runs": {}})
