"""The commit feed is a Lance table the catalog governs, and a follower reads it FORWARD (LH-468, ADR 0158).

Lineage keeps no database: every event it admits is a row of `provenance$events`, written through the
catalog's own `merge_insert` door and read back through its `query` door, and `GET /events?since=` pages that
table oldest first. Driven over real sockets against the real catalog app on a real `dir` namespace, because
the claim is that lineage's store IS the catalog: a live uvicorn catalog announcing each commit over HTTP to
lineage's own app, served beside it, which records it back into that catalog. Recording a commit is itself a
commit, so a catalog that announced its provenance namespace's writes would feed lineage its own rows for ever.

What stands in is `lineage_app`'s: OpenFGA answering from a granted set, and the verified caller named in a
header. The visibility filter is the claim of the second test, and neither stand-in is a claim about the store.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient

from tests.integration.lineage_app import SUBJECT_HEADER, free_port, governed_lineage, live_catalog, live_lineage, run_event


_PIPELINE = "etl-runner"
_ARROW = {"content-type": "application/vnd.apache.arrow.stream"}

#: The tables the pipeline may see. `acme-silver$hidden` and `globex-silver$orders` are someone else's.
_GRANTED = {(_PIPELINE, "can_get_metadata", f"table:{name}") for name in ("acme-silver$pages", "acme-gold$catalog", "acme-gold$index")}


def _rows(start: int, stop: int) -> bytes:
    table = pa.table({"id": pa.array(range(start, stop), pa.int64())})
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _follow(client: httpx.Client, headers: Mapping[str, str] | None = None, **filters: str) -> list[dict[str, Any]]:
    """Walk the feed the way a pipeline does: poll from the last cursor until a poll brings nothing new."""
    seen: list[dict[str, Any]] = []
    cursor = 0
    for _ in range(20):
        response = client.get("/events", params={"since": cursor, "limit": 2, **filters}, headers=dict(headers or {}))
        assert response.status_code == 200, response.text
        page = response.json()
        seen += page["events"]
        if not page["events"] and page["next_cursor"] == cursor:
            return seen
        assert page["next_cursor"] > cursor, f"the cursor must move forward or the walk is over: {page}"
        cursor = page["next_cursor"]
    raise AssertionError(f"the walk did not settle: {seen}")


def _operation(event: dict[str, Any]) -> str:
    """The catalog operation an announcement names: on its run, or on the dataset of a DDL change, which has no run."""
    facets = event["run"]["facets"] if "run" in event else event["dataset"]["facets"]
    return facets["lance"]["operation"]


def test_each_catalog_commit_is_followed_forward_once_and_in_commit_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = free_port()
    announced = {"LANCE_LINEAGE_EMIT_ENABLED": "true", "LANCE_LINEAGE_TRANSPORT": "http", "LANCE_LINEAGE_URL": f"http://127.0.0.1:{port}/api/v1/lineage"}
    with (
        live_catalog(tmp_path / "catalog", monkeypatch, **announced) as catalog_url,
        live_lineage(catalog_url, monkeypatch, port=port) as lineage_url,
        httpx.Client(base_url=catalog_url, timeout=60) as catalog,
        httpx.Client(base_url=lineage_url, timeout=60) as lineage,
    ):
        assert catalog.post("/v1/namespace/acme-silver/create", json={}).status_code == 200
        created = catalog.post("/v1/table/acme-silver$pages/create", content=_rows(0, 3), headers=_ARROW)
        assert created.status_code == 200, created.text
        inserted = catalog.post("/v1/table/acme-silver$pages/insert", content=_rows(3, 5), headers=_ARROW)
        assert inserted.status_code == 200, inserted.text
        # What the outbox relay sends when it recovers an event whose publish was never acknowledged.
        redelivered = lineage.post("/api/v1/lineage", json=_follow(lineage)[-1]["event"])
        assert redelivered.status_code == 201, redelivered.text

        seen = _follow(lineage)
        # A cursor this feed never issued, as a follower holds one after the store is moved or restored.
        ahead = lineage.get("/events", params={"since": 121_000}).json()

    assert [(row["outputs"], _operation(row["event"])) for row in seen] == [(["acme-silver$pages"], "create_table"), (["acme-silver$pages"], "insert")]
    seqs = [row["seq"] for row in seen]
    assert seqs == sorted(set(seqs)), seqs
    assert (ahead["events"], ahead["next_cursor"]) == ([], seqs[-1]), "a cursor this feed never issued must be answered with its newest seq"


@pytest.fixture
def governed_feed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Lineage's router behind its real authentication-then-FGA gate, over a feed the store wrote to the catalog."""
    from lineage.models import parse_event

    feed = [
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:01+00:00", outputs=["acme-silver$pages"]),
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:02+00:00", outputs=["acme-silver$hidden"]),
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:03+00:00", outputs=["acme-gold$catalog"], inputs=("acme-silver$pages",)),
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:04+00:00", outputs=["globex-silver$orders"]),
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:05+00:00", outputs=["acme-gold$index"]),
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:06+00:00", outputs=["acme-gold$catalog"], inputs=("acme-silver$hidden",)),
    ]
    with live_catalog(tmp_path / "catalog", monkeypatch) as url, governed_lineage(url, monkeypatch, _GRANTED) as client:
        store = client.app.state.provenance

        async def _seed() -> None:
            for event in feed:
                await store.record(parse_event(event))

        asyncio.run(_seed())
        yield client


def test_a_table_or_namespace_filter_narrows_the_feed_and_hidden_events_stay_hidden(governed_feed: TestClient) -> None:
    def outputs(rows: list[dict[str, Any]]) -> list[list[str]]:
        return [row["outputs"] for row in rows]

    caller = {SUBJECT_HEADER: _PIPELINE}
    assert outputs(_follow(governed_feed, caller)) == [["acme-silver$pages"], ["acme-gold$catalog"], ["acme-gold$index"]]
    assert outputs(_follow(governed_feed, caller, table="acme-silver$pages")) == [["acme-silver$pages"], ["acme-gold$catalog"]]
    assert outputs(_follow(governed_feed, caller, namespace="acme-gold")) == [["acme-gold$catalog"], ["acme-gold$index"]]
    assert _follow(governed_feed, caller, table="acme-silver$hidden") == []
