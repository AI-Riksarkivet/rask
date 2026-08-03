"""The activities' side effects, in one place — every network call the workflow makes.

Split from `workflow.py` deliberately. Workflow bodies replay from history, so a reader must be able
to see at a glance that they contain no I/O; keeping the actual calls here makes that verifiable
rather than a matter of trust. It also means the workflow module imports nothing heavy at definition
time, which matters because Dapr imports it in every worker to register the definitions.

Config is env-driven and resolved per call rather than at import: an activity may run on any worker
after a replay, and a module-level client captured at import time would outlive the pod it was built
for.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa


if TYPE_CHECKING:
    from ingest.workflow import ChunkSpec, RunSpec

# The bronze schema: the data AS RECEIVED plus the acquisition facts (§3.5). Bronze is the archive's
# copy and the replay foundation, so nothing here decodes or converts.
BRONZE_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64()),
        pa.field("source_uri", pa.string()),
        pa.field("payload", pa.binary()),
    ]
)


def nats_url() -> str:
    return os.getenv("RASK_NATS_URL", "nats://rask-nats:4222")


def warehouse_root() -> str:
    # The env var is the real answer in every deployment; the temp fallback exists so a local run
    # or a test needs no configuration. gettempdir() rather than a literal /tmp so it stays correct
    # off Linux and under a sandbox that relocates TMPDIR.
    return os.getenv("RASK_INGEST_WAREHOUSE") or str(Path(tempfile.gettempdir()) / "rask-ingest")


def dataset_uri(spec: RunSpec) -> str:
    """Resolve {project, dataset} to a location — I2's "no hardcoded dataset paths".

    In-cluster this resolves THROUGH the catalog; the env form is the local/dev fallback. Either way
    the caller never names a path, which is what stops volume B overwriting volume A.
    """
    return f"{warehouse_root().rstrip('/')}/{spec.project}/{spec.dataset}.lance"


async def publish_chunk_units(chunk: ChunkSpec) -> int:
    """Put this chunk's units on the work queue."""
    from ingest.queue import UnitTask, WorkQueue

    queue = await WorkQueue.connect(nats_url())
    try:
        await queue.ensure_stream()
        tasks = [UnitTask(run_id=chunk.run_id, chunk_id=chunk.chunk_id, key=key, dataset_uri=_uri_for_run(chunk.run_id)) for key in chunk.keys]
        return await queue.publish_units(tasks)
    finally:
        await queue.close()


async def reconcile_from_queue(chunk: ChunkSpec) -> dict[str, Any]:
    """Ask the QUEUE what is outstanding — the dead-man's single read.

    With WORK_QUEUE retention an acked unit is gone, so `num_pending == 0` means the chunk really
    drained and only the signal was lost. That is why this needs no ledger to consult: the stream
    IS the ledger.
    """
    from ingest.queue import WorkQueue

    queue = await WorkQueue.connect(nats_url())
    try:
        sub = await queue.subscribe(chunk.run_id)
        info = await sub.consumer_info()
        drained = info.num_pending == 0
        return {
            "chunk_id": chunk.chunk_id,
            "fragments": [],
            "errors": {} if drained else {"__chunk__": f"{info.num_pending} units still outstanding"},
        }
    finally:
        await queue.close()


def finalize_run(spec: RunSpec, fragments: list[str], errors: dict[str, str]) -> dict[str, Any]:
    """Commit the run's fragments as ONE version, through the lander.

    `COMPLETE_WITH_ERRORS` is a real terminal state, not a failure: a run where 3 of 10,000 pages
    were corrupt DID deliver 9,997 pages, and calling that FAILED would either discard good data or
    train operators to ignore the status field.
    """
    from ingest.lander import Lander

    uri = dataset_uri(spec)
    catalog = _catalog()
    # D6 step 1, wired: the dataset is created EMPTY before any fragment is committed. The first
    # in-cluster run failed here with "Dataset at path ... was not found" — the in-process tests call
    # ensure_at() themselves and the WORKFLOW never did, so the creation two-step was documented and
    # unwired. Idempotent, so a replayed finalize activity is a no-op rather than a second create.
    catalog.ensure_at(uri)
    result = Lander(catalog).commit_fragments(uri, fragments, run_id=spec.run_id)
    return {
        "committed_version": result.version,
        "rows": result.rows,
        "errors": errors,
        "status": "COMPLETE_WITH_ERRORS" if errors else "COMPLETE",
    }


def _uri_for_run(run_id: str) -> str:
    """The dataset a run writes to, resolved from the run id alone.

    A worker only ever sees a UnitTask, so the path must be derivable without the RunSpec. Held here
    rather than passed through every layer, so there remains exactly one place that maps a run to a
    location — I2 again.
    """
    return os.getenv("RASK_INGEST_ACTIVE_DATASET", f"{warehouse_root().rstrip('/')}/{run_id}.lance")


def _catalog() -> Any:  # noqa: ANN401 — the real client lands with the catalog commit-through step
    from ingest.catalog import LocalCatalog

    return LocalCatalog(BRONZE_SCHEMA)


def lineage_emitter() -> Any:  # noqa: ANN401
    from ingest.lineage import LineageRecorder

    return LineageRecorder()
