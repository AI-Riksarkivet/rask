"""A committed version whose announcement was lost reaches the feed within one reconcile tick, exactly once ([[LH-497]]).

The catalog announces a write after its commit, and a crash between the commit and the staging of its event loses the
announcement while the version stays committed. Here the loss is real rather than simulated: the catalog announces over
HTTP through a gate that refuses every event while it is down, so each write's own announcement fails and nothing is
staged. One tick of the catalog's reconcile cron then finds each version from the table's version log and announces it,
under the run id derived from the commit, so the write's own announcement, delivered late, lands no second row. A
re-announcement says so (`rask_lance.reannounced`) and a write's own announcement does not: lineage records the catalog's
own announcement on its signature, and checks a re-announcement, whose author only the version log names, as it checks any
producer's event ([[LH-551]]).

A registered table's versions before its register were committed past every door, so none is the catalog's to announce,
whatever ``rask.author`` it carries: the register's own marker is its announcement, and only a commit made through the
catalog after the register is looked for ([[LH-550]]). Here the history is a stock create, an append naming another
estate's person and an append on a branch; on this catalog, which authenticates nobody, each would be announced if it were
looked for, and on one that authenticates the first would be counted unauthored.

Driven over real sockets against the real catalog and lineage apps on a real ``dir`` namespace, as
`test_the_commit_feed_is_a_lance_table_followed_forward.py` drives them: the claim is about what a pipeline following
``/events?since=`` reads. What stands in is the gate, a recording hop in front of lineage, and the paging counter, recorded by a double of its
instrument.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi import Body, FastAPI, Response

from tests.integration.lineage_app import free_port, live_catalog, live_lineage, serve


_ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
_TICK = "catalog-commit-reconcile-cron"


def _rows(start: int, stop: int) -> bytes:
    table = pa.table({"id": pa.array(range(start, stop), pa.int64())})
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _follow(client: httpx.Client) -> list[dict[str, Any]]:
    """Walk the feed the way a pipeline does: from the last cursor until a poll brings nothing new."""
    seen: list[dict[str, Any]] = []
    cursor = 0
    for _ in range(20):
        page = client.get("/events", params={"since": cursor}).json()
        seen += page["events"]
        if not page["events"] and page["next_cursor"] == cursor:
            return seen
        cursor = page["next_cursor"]
    raise AssertionError(f"the walk did not settle: {seen}")


def _commits(rows: list[dict[str, Any]], table: str) -> list[tuple[str, str, str, bool]]:
    """``(table, operation, version, re-announced)`` of each feed row announcing a write of ``table``."""
    found = []
    for row in rows:
        event: Mapping[str, Any] = row["event"]
        for output in event.get("outputs") or []:
            if output["name"] == table:
                lance = event["run"]["facets"]["rask_lance"]
                found.append((output["name"], lance["operation"], output["facets"]["version"]["datasetVersion"], lance.get("reannounced") is True))
    return found


class _Gate:
    """The hop the catalog announces through: it records every event, refuses it while down, and forwards it once up."""

    def __init__(self) -> None:
        self.refused: list[dict[str, Any]] = []
        self.lineage: str | None = None
        self.app = FastAPI()
        self.app.add_api_route("/api/v1/lineage", self._take, methods=["POST"])

    async def _take(self, event: dict[str, Any] = Body()) -> Response:  # noqa: B008 — FastAPI's own body declaration
        if self.lineage is None:
            self.refused.append(event)
            return Response(status_code=503)
        async with httpx.AsyncClient(base_url=self.lineage, timeout=60) as client:
            forwarded = await client.post("/api/v1/lineage", json=event)
        return Response(status_code=forwarded.status_code, content=forwarded.content)


class _Counter:
    """The paging counter's instrument, recording what it is handed."""

    def __init__(self) -> None:
        self.added: list[tuple[int, Mapping[str, str] | None]] = []

    def add(self, amount: int, attributes: Mapping[str, str] | None = None) -> None:
        self.added.append((amount, attributes))


def test_a_lost_announcement_is_on_the_feed_after_one_tick_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from catalog.api import commit_reconcile

    counted = _Counter()
    monkeypatch.setattr(commit_reconcile, "_reannounced", counted)
    gate = _Gate()
    with serve(gate.app) as gate_url:
        env = {
            "LANCE_LINEAGE_EMIT_ENABLED": "true",
            "LANCE_LINEAGE_TRANSPORT": "http",
            "LANCE_LINEAGE_URL": f"{gate_url}/api/v1/lineage",
            "LANCE_COMMIT_RECONCILE_GRACE_SECONDS": "0",
        }
        with (
            live_catalog(tmp_path / "catalog", monkeypatch, **env) as catalog_url,
            httpx.Client(base_url=catalog_url, timeout=60) as catalog,
        ):
            # Mounted as the chart's binding name mounts it at boot (`main.py`), on the app this worker already imported.
            from catalog.main import app

            if not any(getattr(route, "path", None) == f"/{_TICK}" for route in app.routes):
                commit_reconcile.mount_commit_reconcile(app, _TICK)
            assert catalog.post("/v1/namespace/db/create", json={}).status_code == 200
            # The gate is down, so each write's own announcement is lost after its commit.
            assert catalog.post("/v1/table/db$t/create", content=_rows(0, 3), headers=_ARROW).status_code == 200
            assert catalog.post("/v1/table/db$t/insert", content=_rows(3, 5), headers=_ARROW).status_code == 200
            # A table only declared holds no version: nothing to announce, and nothing the tick cannot read.
            assert catalog.post("/v1/table/db$declared/declare", json={}).status_code == 200
            originals = [event for event in gate.refused if "run" in event and event["outputs"][0]["name"] == "db$t"]
            assert len(originals) == 2, gate.refused
            assert [original["run"]["facets"]["rask_lance"].get("reannounced") for original in originals] == [None, None], originals

            with live_lineage(catalog_url, monkeypatch, port=free_port()) as lineage_url, httpx.Client(base_url=lineage_url, timeout=60) as lineage:
                gate.lineage = lineage_url
                assert _commits(_follow(lineage), "db$t") == [], "the writes' own announcements reached the feed, so none was lost"

                # A retained version the store fails to open once: the tick keeps the ref's mark, and skips nothing for it.
                real_checkout = lance.LanceDataset.checkout_version

                def unreadable_once(dataset: lance.LanceDataset, version: int | str | tuple[str | None, int | None]) -> lance.LanceDataset:
                    if version == 1 and str(dataset.uri).endswith(location_of_t):
                        monkeypatch.setattr(lance.LanceDataset, "checkout_version", real_checkout)
                        raise OSError("the store did not answer")
                    return real_checkout(dataset, version)

                location_of_t = str(catalog.post("/v1/table/db$t/describe", json={}).json()["location"]).rstrip("/").rsplit("/", 1)[-1]
                monkeypatch.setattr(lance.LanceDataset, "checkout_version", unreadable_once)
                blocked = catalog.post(f"/{_TICK}")
                assert (blocked.json()["reannounced"], blocked.json()["unreadable"]) == (0, 1), blocked.json()

                tick = catalog.post(f"/{_TICK}")
                assert tick.status_code == 200, tick.text
                assert tick.json()["reannounced"] == 2, tick.json()
                assert _commits(_follow(lineage), "db$t") == [("db$t", "create_table", "1", True), ("db$t", "insert", "2", True)]

                # Each write's own announcement arriving late, as the outbox relay delivers one it had staged.
                for original in originals:
                    late = lineage.post("/api/v1/lineage", json=original)
                    assert late.status_code == 201, late.text
                again = catalog.post(f"/{_TICK}")
                assert (again.json()["reannounced"], again.json()["unreadable"]) == (0, 0), again.json()
                assert _commits(_follow(lineage), "db$t") == [("db$t", "create_table", "1", True), ("db$t", "insert", "2", True)]

                # A dataset written outside the catalog, registered, then written through the catalog while the gate is
                # down again: only that last write's announcement is the catalog's, and lost.
                gate.lineage = None
                outside = str(tmp_path / "catalog" / "outside" / "registered")
                stock = lance.write_dataset(pa.table({"id": pa.array([0, 1], pa.int64())}), outside, data_storage_version="2.2", enable_stable_row_ids=True)
                stock = lance.write_dataset(
                    pa.table({"id": pa.array([2], pa.int64())}), stock, mode="append", transaction_properties={"rask.author": "dex~mallory"}
                )
                stock.create_branch("work")
                lance.dataset(outside).checkout_version(("work", None)).insert(pa.table({"id": pa.array([3], pa.int64())}))
                registered = catalog.post("/v1/table/db$registered/register", json={"location": "outside/registered"})
                assert registered.status_code == 200, registered.text
                assert catalog.post("/v1/table/db$registered/insert", content=_rows(4, 5), headers=_ARROW).status_code == 200
                gate.lineage = lineage_url

                after_register = catalog.post(f"/{_TICK}")
                assert (after_register.json()["reannounced"], after_register.json()["unreadable"]) == (1, 0), after_register.json()
                assert _commits(_follow(lineage), "db$registered") == [("db$registered", "insert", "3", True)]

    assert [amount for amount, _ in counted.added] == [1, 1, 1]
    assert [dict(attributes or {}) for _, attributes in counted.added] == [
        {"lance.catalog.operation": "create_table"},
        {"lance.catalog.operation": "insert"},
        {"lance.catalog.operation": "insert"},
    ]
