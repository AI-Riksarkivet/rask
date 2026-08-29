"""The read-side run cache must be a cache — bounded — not a per-pod leak.

open_python-audit `ingest-flow-15` (E8, med, effort S): `InMemoryRunStore._runs` was a plain dict
that nothing ever deleted from, so the store grew one `RunRecord` per accepted run for the pod's
lifetime, and `recent()` re-sorted the whole of it on every list call.

Eviction is SAFE here, and that is what makes the bound the right fix rather than a durable store:
`record_from_workflow_state` rebuilds an evicted run from the engine's own `serialized_input` on
GET (the same path that answers after a pod restart), so losing an entry costs one extra gRPC call,
never an answer.
"""

from __future__ import annotations

import pytest
from ingest.runs import InMemoryRunStore, RunRecord


def _record(run_id: str) -> RunRecord:
    return RunRecord(run_id=run_id, project="proj", dataset="ds", kind="k")


@pytest.mark.asyncio
async def test_the_store_evicts_its_oldest_run_at_the_cap() -> None:
    store = InMemoryRunStore(max_records=3)
    for i in range(4):
        await store.put(_record(f"run-{i}"))

    assert await store.get("run-0") is None, "the oldest run must be evicted once the cap is exceeded"
    for i in (1, 2, 3):
        assert await store.get(f"run-{i}") is not None
    assert len(await store.recent(10)) == 3


@pytest.mark.asyncio
async def test_updating_a_live_run_refreshes_it_rather_than_evicting_a_neighbour() -> None:
    """A `put` of an existing id is the accept path recording an outcome, not a new run.

    It must not grow the store past the cap, and it must count as recency — the run being actively
    written to is exactly the one a poll is about to read, so it is the WRONG one to evict.
    """
    store = InMemoryRunStore(max_records=2)
    await store.put(_record("run-a"))
    await store.put(_record("run-b"))
    await store.put(_record("run-a").model_copy(update={"scheduled": True}))  # update, not insert
    await store.put(_record("run-c"))  # exceeds the cap: the stalest run (b) goes, not a

    updated = await store.get("run-a")
    assert updated is not None and updated.scheduled is True
    assert await store.get("run-b") is None
    assert await store.get("run-c") is not None


@pytest.mark.asyncio
async def test_the_default_store_carries_a_real_cap() -> None:
    """The bound must hold for the store the app factory actually builds, not only a test's."""
    from ingest.runs import RUN_STORE_MAX_RECORDS

    store = InMemoryRunStore()
    # The private read is deliberate: the cap has no public reader, and adding one for a test alone would be API for nobody.
    assert store._max_records == RUN_STORE_MAX_RECORDS
    assert 0 < RUN_STORE_MAX_RECORDS <= 100_000
