"""[[LH-148]] A parked lineage delivery is kept, and replayed into the feed on demand once its cause is fixed.

The parking route presents a park to the ingest path once, when it arrives; the DLQ consumer (deliverPolicy=new) never
presents it again, the DLQ stream keeps it 168 h, and lineage reads no NATS itself. So the route keeps each delivery it
could not recover in the provenance namespace's ``refusals`` table, and the estate admin re-presents it through
``POST /admin/dlq/parked/{run_id}/replay``: the same authorization and signature check the bus applies, written to the
feed directly, publishing nothing on ``lineage.events.v1``. Each delivery is judged on its own feed row, since a run's
START reaching the feed says nothing of its COMPLETE, the event naming the outputs a missing grant refuses.

Driven through lineage's router over the real store in a live catalog (`lineage_app`), its parking route mounted as
the sidecar delivers to it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from service_kit.governed.fga import user_object
from tests.integration.lineage_app import RELAY, SUBJECT_HEADER, governed_lineage, live_catalog, run_event


_TABLE = "acme-silver$parked"
_RUN = "run-parked"
_ADMIN = "dex~root"
_AUTHOR = "dex~alice"
_TOKEN = {"dapr-api-token": "s3cret"}
_REPLAY = f"/admin/dlq/parked/{_RUN}/replay"


class _Publisher:
    """Lineage's Dapr publisher (``app.state.dapr``), recording what it would put on the bus."""

    def __init__(self) -> None:
        self.published: list[dict[str, object]] = []

    async def publish_event(self, **kwargs: object) -> None:
        self.published.append(kwargs)


def _declared(catalog_url: str, table: str) -> str:
    namespace = table.split("$")[0]
    with httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        assert catalog.post(f"/v1/table/{table}/declare", json={}).status_code == 200
        return str(catalog.post(f"/v1/table/{table}/describe", json={}).json()["properties"]["rask.uuid"])


def _event(state: str, table: str, *, outputs: bool) -> dict[str, Any]:
    """The run's ``state`` event, as the bus delivers it: each output named by the uuid the catalog stamps on it."""
    data = run_event(_RUN, state, "2026-10-27T09:00:00Z", outputs=[_TABLE] if outputs else [], facets={"author": {"sub": _AUTHOR}})
    for output in data["outputs"]:
        output["facets"] = {"lance": {"tableUuid": table.removeprefix("table:")}}
    return data


@contextmanager
def _estate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, str, _Publisher]]:
    """Lineage with its parking route mounted, the estate admin granted, and the table the run writes."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    monkeypatch.setenv("LINEAGE_DAPR_ENABLED", "true")
    monkeypatch.setenv("LINEAGE_DLQ_TOPIC", "dlq.lineage.events")
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url:
        table = f"table:{_declared(catalog_url, _TABLE)}"
        from lineage.api.dapr import register_dapr
        from lineage.core.config import get_settings

        with governed_lineage(catalog_url, monkeypatch, set()) as lineage:
            lineage.app.state.grants.allowed.add((user_object(_ADMIN), "can_observe_events", get_settings().fga_root_object))
            register_dapr(lineage.app)
            publisher = _Publisher()
            lineage.app.state.dapr = publisher
            yield lineage, table, publisher


def _recorded(lineage: TestClient, state: str, table: str) -> bool:
    from lineage.services.provenance import delivery_feed_key

    key = delivery_feed_key({"data": _event(state, table, outputs=True)})
    return key in asyncio.run(lineage.app.state.provenance.events_recorded({key}))


def test_a_refused_complete_is_kept_and_replayed_into_the_feed_once_its_tuple_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _estate(tmp_path, monkeypatch) as (lineage, table, publisher):
        started = lineage.post("/lineage-events", json={"id": "evt-start", "data": _event("START", table, outputs=False)}, headers=_TOKEN)
        parked = lineage.post("/lineage-dlq", json={"id": "evt-complete", "data": _event("COMPLETE", table, outputs=True)}, headers=_TOKEN)
        listed = lineage.get("/admin/dlq/parked", headers={SUBJECT_HEADER: _ADMIN})
        not_admin = lineage.post(_REPLAY, headers={SUBJECT_HEADER: _AUTHOR})
        refused = lineage.post(_REPLAY, headers={SUBJECT_HEADER: _ADMIN})
        lineage.app.state.grants.allowed.add((user_object(_AUTHOR), "can_write_data", table))
        replayed = lineage.post(_REPLAY, headers={SUBJECT_HEADER: _ADMIN})
        again = lineage.post(_REPLAY, headers={SUBJECT_HEADER: _ADMIN})
        left = lineage.get("/admin/dlq/parked", headers={SUBJECT_HEADER: _ADMIN})
        recorded = _recorded(lineage, "COMPLETE", table)

    assert (started.json(), parked.json()) == ({"status": "SUCCESS"}, {"status": "SUCCESS"})
    assert listed.json() == {"runs": {_RUN: 1}}, "a COMPLETE refused after its START landed was not kept"
    assert (not_admin.status_code, "can_observe_events" in not_admin.json()["detail"]) == (403, True), not_admin.text
    assert (refused.status_code, refused.json()["code"]) == (400, 13), refused.text
    assert (replayed.status_code, replayed.json()) == (200, {"status": "recovered", "run_id": _RUN}), replayed.text
    assert (again.status_code, left.json()) == (404, {"runs": {}}), "a recovered park is still kept"
    assert recorded, "the feed lacks the COMPLETE the door recovered"
    assert publisher.published == [], "a replayed park was published again"


def test_the_relay_tick_retires_a_kept_park_the_feed_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A kept delivery whose row reaches the feed by another path (here the producer's own ingest over HTTP) is retired
    by the next relay tick, rather than kept until an admin replays it."""
    with _estate(tmp_path, monkeypatch) as (lineage, table, _):
        parked = lineage.post("/lineage-dlq", json={"id": "evt-complete", "data": _event("COMPLETE", table, outputs=True)}, headers=_TOKEN)
        lineage.app.state.grants.allowed.add((user_object(_AUTHOR), "can_write_data", table))
        ingested = lineage.post("/api/v1/lineage", json=_event("COMPLETE", table, outputs=True), headers={SUBJECT_HEADER: _AUTHOR})
        tick = lineage.post(f"/{RELAY}", headers=_TOKEN)
        left = lineage.get("/admin/dlq/parked", headers={SUBJECT_HEADER: _ADMIN})

    assert (parked.json(), ingested.status_code) == ({"status": "SUCCESS"}, 201), ingested.text
    assert (tick.json()["parks_retired"], left.json()) == (1, {"runs": {}})
