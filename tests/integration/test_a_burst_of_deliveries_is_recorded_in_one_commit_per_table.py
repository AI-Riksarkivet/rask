"""A burst of deliveries is recorded in one commit per provenance table, and every rule of a record holds across it ([[LH-533]]).

Lineage takes the sidecar's deliveries in parallel, and each recorded event touches up to three tables: the run's
binding in `runs`, its edges in `edges`, its row in `events`. Every commit is a Lance version and a fragment each later
open and scan pays for (ADR 0174), so the deliveries that arrive while a commit is in flight land together in the next
one. Driven against the real catalog app under uvicorn on a real `dir` namespace (`lineage_app.live_catalog`), each
delivery a concurrent `ProvenanceStore.record`, as the sidecar's parallel deliveries reach it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from catalog.core.config import get_settings
from lineage.models import parse_event
from lineage.services.provenance import EDGES, FEED, RUNS, ProvenanceStore
from service_kit.lancekit.reader import CatalogTableReader, RestCatalogTransport
from tests.integration.lineage_app import ensure_tables, live_catalog, run_event


_ALICE = "dex~alice"
_BOB = "dex~bob"
_SOURCE = "acme-bronze$src"


def _authored(author: str) -> dict[str, Any]:
    return {"author": {"name": author, "sub": author}}


def _complete(run_id: str, output: str, *, author: str = _ALICE, inputs: tuple[str, ...] = (_SOURCE,), outputs: Sequence[str] = ()) -> dict[str, Any]:
    return run_event(run_id, "COMPLETE", "2026-10-23T10:00:01+00:00", outputs=[output, *outputs], inputs=inputs, facets=_authored(author))


def _store(url: str) -> ProvenanceStore:
    store = ProvenanceStore(catalog_url=url, namespace=["provenance"], token=lambda: None)
    ensure_tables(store)
    return store


def _reader(url: str, table: str) -> CatalogTableReader:
    return CatalogTableReader(RestCatalogTransport(url), ["provenance", table])


def _versions(url: str) -> dict[str, int]:
    return {table: _reader(url, table).table_version() for table in (RUNS, EDGES, FEED)}


def _deliver(store: ProvenanceStore, events: Sequence[dict[str, Any]]) -> list[BaseException | None]:
    """Record each of ``events`` concurrently, as parallel deliveries reach the store; each record's outcome, in order."""

    async def burst() -> list[BaseException | None]:
        return await asyncio.gather(*(store.record(parse_event(event)) for event in events), return_exceptions=True)

    return asyncio.run(burst())


def _feed(store: ProvenanceStore) -> Counter[tuple[str, str]]:
    return Counter((str(record.event["run"]["runId"]), str(record.event_type)) for record in asyncio.run(store.list_events(10_000, since=0)))


def _edges_by_run(url: str) -> Counter[str]:
    return Counter(_reader(url, EDGES).to_table(columns=["run_id"]).column("run_id").to_pylist())


def test_a_burst_lands_in_two_commits_per_table_each_row_once_and_its_run_bound_to_the_first_author(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = [_complete(str(uuid.uuid4()), f"acme-silver$t{n}") for n in range(24)]
    recorded = _complete(str(uuid.uuid4()), "acme-silver$recorded")
    twice = _complete(str(uuid.uuid4()), "acme-silver$twice")
    shared = str(uuid.uuid4())
    started = run_event(shared, "START", "2026-10-23T10:00:00+00:00", outputs=[], facets=_authored(_ALICE))
    finished = _complete(shared, "acme-silver$shared", author=_BOB)
    # A redelivery of a recorded event, two deliveries of one event, and a run whose two events name two authors.
    burst = [*fresh, recorded, twice, twice, started, finished]

    with live_catalog(tmp_path / "catalog", monkeypatch) as url:
        store = _store(url)
        asyncio.run(store.record(parse_event(recorded)))
        before = _versions(url)
        outcomes = _deliver(store, burst)
        after = _versions(url)
        feed = _feed(store)
        edges = _edges_by_run(url)
        author = asyncio.run(store.run_author(shared))

    # The first delivery commits alone and every other one arrives while it is in flight.
    commits = {table: after[table] - before[table] for table in after}
    assert max(commits.values()) <= 2, f"{len(burst)} deliveries took {commits} commits per table"
    assert outcomes == [None] * len(burst)
    completes = [event["run"]["runId"] for event in (*fresh, recorded, twice, finished)]
    assert feed == Counter({**{(run, "COMPLETE"): 1 for run in completes}, (shared, "START"): 1})
    assert edges == Counter(dict.fromkeys(completes, 1))
    assert author == _ALICE


def test_an_event_whose_edges_the_catalog_refuses_fails_alone_and_its_feed_row_never_lands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = [_complete(str(uuid.uuid4()), f"acme-silver$t{n}") for n in range(12)]
    # 64 inputs to 64 outputs, lineage's bound on each (`lineage.models.MAX_DATASETS`): 4,096 edges, about 1 MB of rows
    # against a catalog whose body cap is 256 KiB, while the event itself weighs about 15 kB.
    wide = _complete(
        str(uuid.uuid4()),
        "acme-silver$w0",
        inputs=tuple(f"acme-bronze$i{n}" for n in range(64)),
        outputs=[f"acme-silver$w{n}" for n in range(1, 64)],
    )
    burst = [*fresh[:6], wide, *fresh[6:]]

    with live_catalog(tmp_path / "catalog", monkeypatch) as url:
        store = _store(url)
        # Set once the app is built, for the cap its doors read per request: the body-limit middleware takes its own
        # when `catalog.main` is first imported, and would hold this one for every later test in the worker.
        monkeypatch.setenv("LANCE_MAX_BODY_BYTES", str(256 * 1024))
        get_settings.cache_clear()
        outcomes = _deliver(store, burst)
        feed = _feed(store)
        edges = _edges_by_run(url)

    assert [outcome is None for outcome in outcomes] == [True] * 6 + [False] + [True] * 6
    runs = [event["run"]["runId"] for event in fresh]
    # The edges commit before the feed row that asserts them, so the refused event left neither.
    assert feed == Counter({(run, "COMPLETE"): 1 for run in runs})
    assert edges == Counter(dict.fromkeys(runs, 1))
