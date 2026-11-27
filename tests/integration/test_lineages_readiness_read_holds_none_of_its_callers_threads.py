"""Lineage's readiness read holds none of the threads its callers' calls through the catalog do ([[LH-578]]).

Every call lineage makes through the catalog for a door, for its ingest lane or for its relay runs on one thread set
(`lineage.services.catalog_client.CatalogClients.calls`), and the read its readiness rests on, the feed's current version, runs
on the process's own. Queued behind a burst of callers holding every one of those threads on a catalog slow to answer,
the read would outlast the health watch's budget, and the replica would leave the Service when it is busiest.

Driven on lineage's real provenance store over a live catalog, the callers' threads held as such a burst holds them.
"""

import asyncio
import threading
from pathlib import Path

import pytest

from lakehouse.process.own_threads import ThreadSet
from lineage.core.config import get_settings
from lineage.services.catalog_client import CatalogClients
from lineage.services.provenance import ProvenanceStore
from tests.integration.lineage_app import ensure_tables, live_catalog


#: The health watch's budget for one read of a dependency (`lakehouse.process.health`).
BUDGET_SECONDS = 5.0


async def _read_with_every_catalog_call_thread_held(store: ProvenanceStore, calls: ThreadSet, own: ThreadSet) -> tuple[bool, BaseException | None]:
    """Whether the readiness read answered inside the budget while a burst held every thread of lineage's catalog calls,
    and what it raised."""
    release = threading.Event()
    entered: list[int] = []

    def hold() -> None:
        entered.append(threading.get_ident())
        release.wait(timeout=60)

    burst = [asyncio.create_task(calls.run(hold)) for _ in range(calls.seats)]
    try:
        async with asyncio.timeout(10):
            while len(entered) < calls.seats:
                await asyncio.sleep(0.005)
        reading = asyncio.create_task(store.ready(own))
        answered, _ = await asyncio.wait({reading}, timeout=BUDGET_SECONDS)
        in_time = reading in answered
    finally:
        release.set()
        await asyncio.gather(*burst, return_exceptions=True)
    await asyncio.wait({reading})
    return in_time, reading.exception()


def test_the_readiness_read_answers_while_every_thread_of_the_catalog_calls_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_threads: ThreadSet
) -> None:
    with live_catalog(tmp_path, monkeypatch) as catalog_url:
        monkeypatch.setenv("LINEAGE_CATALOG_URL", catalog_url)
        get_settings.cache_clear()
        clients = CatalogClients(catalog_url)
        store = ProvenanceStore.from_settings(get_settings(), clients)
        ensure_tables(store)

        in_time, failure = asyncio.run(_read_with_every_catalog_call_thread_held(store, clients.calls, own_threads))
        clients.close()
    get_settings.cache_clear()

    assert (in_time, failure) == (True, None), f"lineage's readiness read waited behind the {clients.calls.seats} threads its callers held"
