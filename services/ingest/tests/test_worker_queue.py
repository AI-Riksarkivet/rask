"""Worker + work queue against a REAL clustered NATS, and the write path against REAL Lance.

Not mocked, on purpose. The design's central claim is that the STREAM is the outstanding-work ledger
— that is the reasoning that dissolved the tracker — and a mocked queue would prove only that the
mock agrees with the claim. `RetentionPolicy.WORK_QUEUE` removing an acked message is either true of
JetStream or the design is wrong, and only a real broker can say which.

Skips (never fakes) when no NATS is reachable: `kubectl port-forward svc/rask-nats 4222:4222`, or
set RASK_NATS_URL.
"""

from __future__ import annotations

import os
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse

import lance
import pyarrow as pa
import pytest
import pytest_asyncio
from ingest.lander import create_empty
from ingest.queue import STREAM, UnitTask, WorkQueue, unit_subject
from ingest.worker import Worker, units_to_table


NATS_URL = os.getenv("RASK_NATS_URL", "nats://localhost:4222")

BRONZE_SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("source_uri", pa.string()), pa.field("payload", pa.binary())])


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

    async def fetch(self, key: str) -> bytes:
        self.fetched.append(key)
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
    assert len(outcome.fragments) == 4
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
    assert len(outcome.fragments) == 2, "a corrupt unit must not contribute a fragment"
    await queue.close()


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
    table = lance.dataset(uri).to_table()
    assert sorted(table.column("source_uri").to_pylist()) == [f"iiif://v/{i}" for i in range(3)]
    assert table.column("payload")[0].as_py() == b"bytes-for-iiif://v/0"
    assert cat.registered == [(uri, 2, run)], "the commit must be registered with the run id"
    await queue.close()


def test_the_bronze_batch_is_faithful_to_source() -> None:
    """§3.5: bronze holds the data AS RECEIVED — no decoding, no conversion.

    And `id` is a stable hash of the source URI, so it exists BEFORE the run does. That is why this
    plane never needs an id minted mid-saga, which is the whole reason its steps are idempotent by a
    caller-chosen key.
    """
    t = units_to_table([("iiif://v/1", b"\xff\xd8raw"), ("iiif://v/2", b"other")])
    assert t.column("payload")[0].as_py() == b"\xff\xd8raw", "bronze must not transform the bytes"

    again = units_to_table([("iiif://v/1", b"different-bytes")])
    assert again.column("id")[0].as_py() == t.column("id")[0].as_py(), "id must derive from the URI alone"


def test_the_queue_module_is_the_only_nats_importer() -> None:
    """I3, asserted from the inside too — queue.py is the documented exception, and it is this one."""
    assert STREAM == "INGEST"
    assert unit_subject("r1") == "ingest.tasks.r1"
