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


def _rows_in(fragments_json: list[str]) -> int:
    """Physical rows across a run's fragments — its OWN contribution, independent of the tier."""
    import json

    total = 0
    for blob in fragments_json:
        try:
            total += int(json.loads(blob).get("physical_rows") or 0)
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            continue
    return total


def ensure_dataset_at(spec: RunSpec) -> str:
    """Create the run's dataset empty if absent, and return the URI to write into. D6 step 1.

    The URI is whatever the CATALOG says it is. In-cluster that is a location the catalog vends;
    locally it is composed from the warehouse env. Either way the caller never names a path — I2's
    "no hardcoded dataset paths", which exists because two callers composing the same logical table
    from different env is how volume B overwrote volume A.
    """
    return _catalog().ensure(spec.project, spec.dataset)


async def publish_chunk_units(chunk: ChunkSpec) -> int:
    """Put this chunk's units on the work queue."""
    from ingest.queue import UnitTask, WorkQueue

    queue = await WorkQueue.connect(nats_url())
    try:
        await queue.ensure_stream()
        tasks = [UnitTask(run_id=chunk.run_id, chunk_id=chunk.chunk_id, key=key, dataset_uri=chunk.dataset_uri) for key in chunk.keys]
        return await queue.publish_units(tasks)
    finally:
        await queue.close()


async def drain_chunk_units(chunk: ChunkSpec) -> dict[str, Any]:
    """Run a worker over this chunk until its units are accounted for.

    The fetcher is SCHEME-resolved (`ingest.fetch.UriFetcher`), so a worker needs no source spec —
    only the key its task already carries. That is what keeps I1's "one adapter, one registry entry"
    claim true at the far end of the queue: a new source kind producing `s3://` or `https://` keys
    needs no worker change at all.

    The validator is `packages/validate`, a package with zero consumers since it was written. A
    corrupt TIFF becomes a tracked error and a DLQ entry here, rather than a poisoned row that fails
    months later at read time in someone else's job.
    """
    from ingest.fetch import UriFetcher
    from ingest.queue import WorkQueue
    from ingest.validation import PayloadValidator
    from ingest.worker import Worker

    queue = await WorkQueue.connect(nats_url())
    try:
        await queue.ensure_stream()
        worker = Worker(queue, UriFetcher(), PayloadValidator(), name=chunk.chunk_id)
        outcome = await worker.drain_chunk(chunk.run_id, chunk.chunk_id, len(chunk.keys), chunk.dataset_uri)
        return outcome.model_dump()
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
    from ingest.lander import CommitResult, Lander
    from ingest.staging import discover_staged, purge_staged

    catalog = _catalog()
    uri = catalog.ensure(spec.project, spec.dataset)
    # STORAGE TRUTH, not the workflow's carried value. Fragments staged by a drain attempt that died
    # before returning are still on the store and still uncommitted — invisible to `fragments`, which
    # only holds what the surviving attempts handed back. Reading the staging prefix is what turns a
    # mid-run pod death from silent row loss into a slower run (A3). The union is order-preserving
    # and deduplicated, so a fragment reported through both paths commits exactly once.
    staged = discover_staged(uri, spec.run_id)
    seen: set[str] = set()
    all_fragments = [f for f in [*staged, *fragments] if not (f in seen or seen.add(f))]

    if hasattr(catalog, "commit"):
        # THE CATALOG COMMITS. A commit registered only in this process is one the cascade cannot
        # ride: the event that wakes a mover is the catalog's publication of a new version, so a
        # locally-recorded commit lands the data and tells nothing downstream it happened.
        version, tier_rows = catalog.commit(
            spec.project,
            spec.dataset,
            all_fragments,
            read_version=catalog.describe_version(spec.project, spec.dataset),
            run_id=spec.run_id,
        )
        # `row_count` from the catalog is the DATASET's total after the commit, not this run's work —
        # the same distinction the Lander path already draws, and it came straight back the moment the
        # catalog path bypassed that code: a second run against one dataset reported 8 units done for
        # 4 ingested files. Counted here from the committed FRAGMENTS, which is also the only form
        # that stays right under concurrent commits.
        added = _rows_in(all_fragments)
        result = CommitResult(dataset_uri=uri, version=version, rows=tier_rows, rows_added=added, fragments_committed=len(all_fragments))
    else:
        result = Lander(catalog).commit_fragments(uri, all_fragments, run_id=spec.run_id)
    # Only after the commit lands. Purging earlier would delete the record a retried finalize needs,
    # turning a recoverable failure into exactly the data loss staging exists to prevent.
    purge_staged(uri, spec.run_id)
    return {
        "committed_version": result.version,
        # THIS run's rows. `result.rows` is the dataset total, which is the same number only for a
        # tier's first run — the second in-cluster lane reported 8 units done for 4 ingested files
        # because the two were conflated.
        "rows": result.rows_added,
        "dataset_rows": result.rows,
        "errors": errors,
        "status": "COMPLETE_WITH_ERRORS" if errors else "COMPLETE",
    }


def _catalog() -> Any:  # noqa: ANN401 — LocalCatalog or CatalogServiceClient, one seam
    from ingest.catalog_service import build_catalog

    return build_catalog(BRONZE_SCHEMA)


def lineage_emitter() -> Any:  # noqa: ANN401
    from ingest.lineage import LineageRecorder

    return LineageRecorder()
