"""Worker + work queue against a REAL clustered NATS, and the write path against REAL Lance.

Not mocked, on purpose. The design's central claim is that the STREAM is the outstanding-work ledger
— that is the reasoning that dissolved the tracker — and a mocked queue would prove only that the
mock agrees with the claim. `RetentionPolicy.WORK_QUEUE` removing an acked message is either true of
JetStream or the design is wrong, and only a real broker can say which.

Skips (never fakes) when no NATS is reachable: `kubectl port-forward svc/rask-nats 4222:4222`, or
set RASK_NATS_URL.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse

import lance
import nats
import pyarrow as pa
import pytest
import pytest_asyncio

from ingest.lander import create_empty
from ingest.queue import DLQ_SUBJECT, STREAM, UnitTask, WorkQueue, unit_subject
from ingest.runtime import BRONZE_SCHEMA, _rows_in
from ingest.worker import Worker, units_to_table


NATS_URL = os.getenv("RASK_NATS_URL", "nats://localhost:4222")


def _reachable() -> bool:
    p = urlparse(NATS_URL)
    try:
        with socket.create_connection((p.hostname or "localhost", p.port or 4222), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason=f"no NATS at {NATS_URL}")


class _StubFetcher:
    """Returns deterministic bytes per key, and can be told to fail one."""

    def __init__(self, fail: set[str] | None = None, corrupt: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.corrupt = corrupt or set()
        self.fetched: list[str] = []
        #: The object store each unit named — None for a run on the deployment's own.
        self.endpoints: list[str | None] = []

    async def fetch(self, key: str, *, source_endpoint: str | None = None) -> bytes:
        self.fetched.append(key)
        self.endpoints.append(source_endpoint)
        if key in self.fail:
            raise RuntimeError(f"transient failure for {key}")
        if key in self.corrupt:
            return b""
        return f"bytes-for-{key}".encode()


# @pytest_asyncio.fixture, NOT @pytest.fixture: rask does not set asyncio_mode="auto", so a
# plain @pytest.fixture on an async function yields the COROUTINE rather than the value.
@pytest_asyncio.fixture
async def queue() -> WorkQueue:
    q = await WorkQueue.connect(NATS_URL)
    await q.ensure_stream()
    # ASK FOR THE DLQ, do not inherit it. The poison-park test below publishes to `dlq.ingest.tasks`,
    # which `ensure_stream` does not cover — it creates INGEST (`ingest.>`) only. That test passed for
    # as long as it did because the dev cluster's chart Job had already created `dlq.>` for the whole
    # estate, so the suite was reading state it never requested and never asserted. Against a bare
    # broker it failed at once with `NoStreamResponseError` (reproduced 2026-08-15 on a clean NATS
    # brought up as a Dagger service). A test whose outcome depends on who else ran first is not
    # testing what it says it tests.
    await q.ensure_dlq_stream()
    return q


@pytest.mark.asyncio
async def test_the_stream_is_the_outstanding_work_ledger(queue: WorkQueue, tmp_path: Path) -> None:
    """The claim that dissolved the tracker: acked units LEAVE the stream.

    If WORK_QUEUE retention did not remove acked messages, "what is outstanding" would need a side
    ledger — which is exactly what packages/tracker was for. This asserts the property directly.
    """
    run = f"r{uuid.uuid4().hex[:8]}"
    uri = str(tmp_path / "bronze.lance")
    create_empty(uri, BRONZE_SCHEMA)

    tasks = [UnitTask(run_id=run, chunk_id="c0", key=f"iiif://v/{i}", dataset_uri=uri) for i in range(4)]
    assert await queue.publish_units(tasks) == 4

    fetcher = _StubFetcher()
    outcome = await Worker(queue, fetcher, name="w1").drain_chunk(run, "c0", expected=4, dataset_uri=uri)

    assert outcome.units_done == 4
    # ROWS, not fragments. This asserted `len(fragments) == 4` — one fragment per unit, which is the
    # defect `test_fragment_batching.py` was written to kill (500 pages meant 500 fragments in one
    # commit). The invariant that survived batching is that every drained unit reaches the dataset;
    # how many fragments carry them is a sizing decision, not a correctness one.
    assert _rows_in(outcome.fragments) == 4
    assert sorted(fetcher.fetched) == sorted(t.key for t in tasks)

    # The ledger property: nothing outstanding remains after every unit was acked. Re-attaching to
    # the run's SHARED durable is how this is checked — a work-queue stream refuses a second
    # filtered consumer on the same subject (err_code 10100), which is exactly why the durable is
    # per-run and not per-worker.
    sub = await queue.subscribe(run)
    info = await sub.consumer_info()
    assert info.num_pending == 0, "acked units did not leave the stream — the queue is not the ledger"
    await queue.close()


@pytest.mark.asyncio
async def test_a_corrupt_unit_parks_and_does_not_poison_the_run(queue: WorkQueue, tmp_path: Path) -> None:
    """A5: validation refuses the byte, the unit is an ERROR, and the run still drains.

    Redelivering corrupt bytes cannot help, so a refused unit is acked and parked rather than
    retried — and the chunk must still complete, or the workflow waits forever on a drain that
    cannot come.
    """
    run = f"r{uuid.uuid4().hex[:8]}"
    uri = str(tmp_path / "bronze.lance")
    create_empty(uri, BRONZE_SCHEMA)

    tasks = [UnitTask(run_id=run, chunk_id="c0", key=f"iiif://v/{i}", dataset_uri=uri) for i in range(3)]
    await queue.publish_units(tasks)

    fetcher = _StubFetcher(corrupt={"iiif://v/1"})
    outcome = await Worker(queue, fetcher, name="w1").drain_chunk(run, "c0", expected=3, dataset_uri=uri)

    assert outcome.units_done == 2
    assert "iiif://v/1" in outcome.errors
    # Again rows, not fragments — and here the row count is the whole point of the test. The refused
    # unit must leave NO trace in the data, and with batching the two survivors share one fragment,
    # so a fragment count can no longer tell a parked unit from a written one.
    assert _rows_in(outcome.fragments) == 2, "the corrupt unit contributed a ROW — parking did not refuse the write"

    # AND THE PARKED UNIT IS ON THE DLQ. Everything above proves the run survived the poison; none of
    # it proved the unit was PARKED, which is the other half of the docstring's claim and the whole
    # point of `park_poison` — "so it is visible rather than merely gone". Replacing the DLQ publish
    # with `pass` left every assertion above green.
    parked = await _parked_units_for(run)
    assert [p["task"]["key"] for p in parked] == ["iiif://v/1"], (
        f"the DLQ holds {[p['task']['key'] for p in parked]} for this run — the refused unit must be there, and nothing else may be"
    )
    assert parked[0]["reason"], "a parked unit carries no reason — it is gone, not visible"
    await queue.close()


async def _parked_units_for(run_id: str) -> list[dict]:
    """Every parked unit belonging to ONE run, read back off `dlq.ingest.tasks`.

    Filtered by `run_id` on purpose. The DLQ is a `limits`-retention stream shared by the whole estate
    — Dapr parks every app's dead letters on `dlq.>` — so it also holds other services' messages and
    every earlier run of this suite. Asserting on its raw contents would be the same borrowed-state
    mistake this test was just fixed for, one level down.

    Opens its OWN connection rather than borrowing the fixture's `WorkQueue`: the queue exposes no
    JetStream handle, and reaching into `_js` from a test would couple the assertion to a private
    attribute for no gain.
    """
    out: list[dict] = []
    nc = await nats.connect(NATS_URL)
    try:
        sub = await nc.jetstream().pull_subscribe(DLQ_SUBJECT)
        while True:
            try:
                msgs = await sub.fetch(batch=64, timeout=1)
            except TimeoutError:
                break  # drained — the terminating condition, not a failure
            for msg in msgs:
                payload = json.loads(msg.data)
                if payload.get("task", {}).get("run_id") == run_id:
                    out.append(payload)
                await msg.ack()
    finally:
        await nc.close()
    return out


@pytest.mark.asyncio
async def test_fragments_written_by_the_worker_commit_through_the_lander(queue: WorkQueue, tmp_path: Path) -> None:
    """End of the write path: worker fragments -> lander -> ONE version, rows readable."""
    from ingest.lander import Lander

    run = f"r{uuid.uuid4().hex[:8]}"
    uri = str(tmp_path / "bronze.lance")
    create_empty(uri, BRONZE_SCHEMA)

    await queue.publish_units([UnitTask(run_id=run, chunk_id="c0", key=f"iiif://v/{i}", dataset_uri=uri) for i in range(3)])
    outcome = await Worker(queue, _StubFetcher(), name="w1").drain_chunk(run, "c0", 3, uri)

    class _Cat:
        def __init__(self) -> None:
            self.registered: list[tuple[str, int, str]] = []

        def ensure_dataset(self, project: str, dataset: str, schema: pa.Schema) -> str:
            return uri

        def register_version(self, dataset_uri: str, version: int, run_id: str) -> None:
            self.registered.append((dataset_uri, version, run_id))

    cat = _Cat()
    result = Lander(cat).commit_fragments(uri, outcome.fragments, run_id=run)

    assert result.rows == 3
    dataset = lance.dataset(uri)
    table = dataset.to_table()
    assert sorted(table.column("source_uri").to_pylist()) == [f"iiif://v/{i}" for i in range(3)]

    # The payload comes back through `read_blobs`, not off the row. A written blob cell holds a
    # DESCRIPTOR (`kind`/`position`/`size`/`blob_id`/`blob_uri`) and no bytes at all, whatever tier
    # it landed in — so `table.column("payload")[0].as_py()` can never assert fidelity, only that a
    # descriptor exists. This asserts the bytes SURVIVED the write, which is what the write path is
    # for. `read_blobs` keys its tuples by ROW INDEX (measured), so the pairing is positional.
    blobs = dict(dataset.read_blobs("payload", indices=list(range(3))))
    fetched = {uri_: blobs[i] for i, uri_ in enumerate(table.column("source_uri").to_pylist())}
    assert fetched == {f"iiif://v/{i}": f"bytes-for-iiif://v/{i}".encode() for i in range(3)}
    assert cat.registered == [(uri, 2, run)], "the commit must be registered with the run id"
    await queue.close()


def test_the_bronze_batch_is_faithful_to_source() -> None:
    """§3.5: bronze holds the data AS RECEIVED — no decoding, no conversion.

    And `id` is a stable hash of the source URI, so it exists BEFORE the run does. That is why this
    plane never needs an id minted mid-saga, which is the whole reason its steps are idempotent by a
    caller-chosen key.
    """
    t = units_to_table([("iiif://v/1", b"\xff\xd8raw"), ("iiif://v/2", b"other")])
    # `payload` is a BLOB column (`blob_array`), so the cell is a descriptor struct rather than the
    # bytes. Before a write the bytes live in its `data` field; this table has never been written.
    assert t.column("payload")[0].as_py()["data"] == b"\xff\xd8raw", "bronze must not transform the bytes"

    again = units_to_table([("iiif://v/1", b"different-bytes")])
    assert again.column("id")[0].as_py() == t.column("id")[0].as_py(), "id must derive from the URI alone"


def test_the_queue_module_is_the_only_nats_importer() -> None:
    """I3, asserted from the inside too — queue.py is the documented exception, and it is this one."""
    assert STREAM == "INGEST"
    assert unit_subject("r1") == "ingest.tasks.r1"


def test_the_ack_ceiling_exceeds_the_fragment_batch() -> None:
    """A deadlock that would look exactly like a slow source.

    The worker holds a whole fragment's worth of messages UNACKED while it accumulates them (that is
    what makes the ack a per-batch promise). If `max_ack_pending` is below the batch size, JetStream
    stops delivering at the ceiling and the drain waits forever for units it will never be sent. At
    the old values — ceiling 32, batch 1024 — every run would have hung on its 33rd unit.

    Asserted as a RELATION between the two, so raising the batch without raising the ceiling fails
    here instead of in a cluster at 3am. Since sizing became per-run this covers the DEFAULT; the
    caller-supplied path is refused at accept (`test_fragment_batching.py`).
    """
    from ingest.queue import max_ack_pending
    from ingest.sizing import resolve

    rows, ceiling = resolve().fragment_rows, max_ack_pending()
    assert rows < ceiling, (
        f"max_ack_pending={ceiling} <= fragment batch {rows}: the drain will deadlock once a batch fills, because the held messages are never acked"
    )
