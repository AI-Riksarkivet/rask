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

import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa
from lance import blob_field


if TYPE_CHECKING:
    from ingest.workflow import ChunkSpec, RunSpec

# The bronze schema: the data AS RECEIVED plus the acquisition facts (§3.5). Bronze is the archive's
# copy and the replay foundation, so nothing here decodes or converts.
#
# `payload` is a BLOB column. It was `pa.binary()`, which forces every page image INLINE into the
# `.lance` data file and gives up all three of Lance's placement tiers — and the code this plane
# replaced already used `blob_field` (`medallion/services/ingest.py:31`), so this was a regression
# against knowledge the estate had already paid for.
#
# What the tiers buy, per `docs/architecture/lance-blob-v2-findings.md` (measured, not read):
#
#   inline     < inline_size_threshold   bytes sit in the .lance file with the other columns
#   packed     between the two           many payloads share one .blob sidecar — this is what stops
#                                        a million mid-sized pages becoming a million S3 objects
#   dedicated  >= dedicated_threshold    the payload gets its OWN .blob file, REFERENCED rather than
#                                        re-copied by every compaction of the fragment
#
# Evaluation is dedicated-FIRST, then inline (counterintuitive, and stated in the docstring).
# Thresholds are left at Lance's defaults (16 KiB / 2 MiB): a scanned archival page lands in
# `dedicated`, a thumbnail or a born-digital text page lands in `packed` or `inline`, which is the
# behaviour we want and did not have to invent.
#
# Placement is transparent to readers — the shapes round-trip identically through `read_blobs`,
# `take_blobs` and `read_blob_ranges` — so this is a pure write-side choice that can be retuned for
# new datasets without touching a reader. MEASURED against a real dataset on RustFS, not assumed:
# `tests/test_blob_read_apis.py` exercises all three, including a `BlobFile` seek + partial read.
#
# `payload` is NON-NULLABLE, and that is a correctness guard rather than a schema opinion.
#
# All three read APIs SILENTLY DROP a null row — measured on pylance 9.0.0 against this schema:
#
#   read_blobs(indices=[0,1,2])       -> 2 tuples. Survivable: each carries its id.
#   take_blobs(indices=[0,1,2])       -> 2 handles in a BARE LIST with NO row identity at all.
#   read_blob_ranges(idx=1, ...)      -> [] for a null row. No error.
#
# `take_blobs` is the dangerous one. A reader doing the obvious `handles[i]` for row `i` silently
# gets a DIFFERENT row's bytes the moment one null exists earlier in the request — a page displayed
# under the wrong page's identity, with nothing raised anywhere. Declaring the column non-nullable
# makes that state unreachable at the source: Lance refuses the write outright
# (`Column 'payload' is declared as non-nullable but contains null values`), so a bug that would
# have surfaced as quietly-wrong data surfaces as a failed ingest instead.
#
# Nothing is lost by it — the plane cannot produce a null payload anyway. `AcceptAll.check` refuses
# an empty payload ("empty payload") and the worker parks it, so a unit either lands with bytes or is
# recorded in `errors`. The old `nullable=True` advertised a state this plane never creates, and it
# was exactly the state that makes every read API lie.
#
# `stage` is the tier provenance stamp every governed dataset in the estate carries. The head this
# plane replaced wrote it AT INGEST — it absorbed the retired raw→bronze mover when R23 made bronze
# the first governed tier — and dropping it was recorded here as a cheap, non-fatal loss because the
# movers' `_stamp_stage` appends the column when it is absent.
#
# That was wrong, and measurably so. The READ path hard-requires it: the media viewer projects
# `_PAGE_COLUMNS = ["id", "source_uri", "stage"]` (`viewer/api/v1/endpoints/pages.py:37`), and that
# projection is not inside its try/except — so every bronze table this plane wrote 500'd on
# `GET /api/pages` and `GET /api/page`:
#
#   Invalid user input: Schema error: No field named stage. Valid fields are id, source_uri.
#
# Every dataset this plane produced was unreadable by the only reader the estate has, and the ingest
# lane's own gates could not see it because they read the dataset directly. A dropped column is only
# cheap until something projects it.
# `sha256` is the FIXITY column (#99 — #92's rule extended to THIS plane, which had superseded the
# medallion head without inheriting it): a hex SHA-256 over the bytes AS FETCHED, computed before
# the write and never recomputed — a digest taken from the stored copy would agree with that copy
# however corrupt it is, which is precisely the failure fixity exists to catch. Same column name as
# the medallion writer's, so the movers' generic carry-forward keeps gold rows traceable to the
# exact page bytes they were read from whichever head landed them (pinned by
# tests/unit/test_bronze_writers_compat.py).
BRONZE_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64()),
        pa.field("source_uri", pa.string()),
        blob_field("payload", nullable=False),
        pa.field("sha256", pa.string()),
        pa.field("stage", pa.string()),
    ]
)

#: What `stage` holds at ingest. Bronze is the first GOVERNED tier (R23: raw is the external world),
#: so the cascade's later movers re-stamp their own tier as the rows move up.
BRONZE_STAGE = "bronze"


#: Publish failures are logged rather than raised — a landed commit must not become a failed run.
_log = logging.getLogger(__name__)


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
    # STORAGE TRUTH, and it is the ONLY truth. Fragments staged by a drain attempt that died before
    # returning are still on the store and still uncommitted — invisible to `fragments`, which holds
    # only what the surviving attempts handed back. Reading the staging prefix is what turns a mid-run
    # pod death from silent row loss into a slower run (A3).
    #
    # `discover_staged` does not merely LIST: it searches for an EXACT COVER of the run's units and
    # deliberately DESELECTS a fragment whose rows another fragment already covers. This used to be
    # unioned with the workflow's carried list — `[*staged, *fragments]`, deduplicated by string —
    # and that silently overruled the selection. Every carried fragment was staged first
    # (`worker.py`: `stage_fragments(...)` is the line immediately before `outcome.fragments.extend`),
    # so the carried list can contribute exactly one thing the selection does not already account
    # for: a fragment the selection SUPERSEDED. Adding it back commits both, which is the "four units
    # in, six rows out" duplication `tests/test_partial_ack_duplication.py` closed — reintroduced one
    # layer above the layer that closed it.
    all_fragments = discover_staged(uri, spec.run_id)
    if not all_fragments and fragments:
        # Staging returned nothing while the workflow is holding fragments. That is not the ordinary
        # empty case (no work), it means the staging prefix was unreadable or its manifests were all
        # truncated — the run's own record of what it wrote is gone. Committing the carried list is
        # the loss-avoiding choice, but it is NOT the exact cover, so say so loudly rather than let a
        # silent fallback look like the normal path.
        _log.warning(
            "ingest_staging_unreadable_using_carried_fragments",
            extra={"run_id": spec.run_id, "dataset_uri": uri, "carried": len(fragments)},
        )
        seen: set[str] = set()
        all_fragments = [f for f in fragments if not (f in seen or seen.add(f))]

    if not all_fragments:
        # NOTHING TO COMMIT IS A NO-OP, NOT A COMMIT OF NOTHING — and this guard exists because the
        # catalog branch below skipped the one `Lander.commit_fragments` has always had
        # (`lander.py:95-100`: "a run whose every unit failed should leave no version behind to
        # explain"). It POSTed `{"fragments": []}`, which the catalog refuses with 400 "no fragments
        # to commit" (`catalog/services/dataplane.py:598`).
        #
        # DEPLOYED, that 400 is a crash, not a message: `RASK_INGEST_USE_CATALOG: "true"`
        # (chart/values.yaml), so the 400 raises out of the `finalize` ACTIVITY, burns its four
        # ACTIVITY_RETRY attempts against a permanently-failing input, and kills the workflow BEFORE
        # `emit_terminal` (workflow.py) — so the run's own FAIL never reaches the lineage graph and
        # the START emitted at accept is orphaned forever. The run reports FAILED with an empty
        # `errors` dict and no operator-readable reason.
        #
        # STRUCTURALLY INVISIBLE TO THE SUITE: this branch runs only when the catalog has `commit`,
        # and `LocalCatalog` — the default with `RASK_INGEST_USE_CATALOG` unset, which is what every
        # test uses — does not. No local test could take it. That is the argument for the guard
        # sitting here rather than inside either catalog implementation.
        #
        # TWO ordinary paths reach it: a source that enumerated zero units, and a run whose every
        # unit failed validation.
        result = Lander(catalog).commit_fragments(uri, all_fragments, run_id=spec.run_id)
        # STILL PURGED. A run whose staged manifests were all truncated (`staging.py` skips those)
        # arrives here with an empty list and would strand its staged bytes with nothing left to
        # collect them.
        purge_staged(uri, spec.run_id)
        return {
            # NOT `result.version`. That is the version the dataset ALREADY had — the previous run's,
            # or the empty v1 `ensure_dataset` created — and reporting it is the "committed_version
            # it did not produce" half of this defect.
            "committed_version": None,
            "rows": 0,
            "dataset_rows": result.rows,
            "errors": errors,
            # UNCHANGED derivation, deliberately: `test_run_chain.py` drives exactly
            # `finalize_run(spec, [], {...})` and pins COMPLETE_WITH_ERRORS under "a run that
            # delivered 9,997 of 10,000 pages did not FAIL". Refusing a genuinely EMPTY SOURCE is a
            # different decision at a different seam (enumeration), not this one.
            "status": "COMPLETE_WITH_ERRORS" if errors else "COMPLETE",
            # No publication: there is no version to gate, and `_publish` would move `published`
            # onto a version this run did not write.
            "published": None,
            "from_version": None,
            "to_version": None,
            "publish_reason": "nothing to commit",
            "publish_error": None,
        }

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

    # A COMMIT IS NOT A PUBLICATION (§ D2 D-R1). The rows are now readable, and until the catalog
    # gates this version and advances `published` they are not READY — nothing downstream should act
    # on them. The plane asks; it never moves the tag itself, because publication has to be one
    # operation shared by every writer or the contract drifts per writer.
    #
    # A REFUSED gate is not a failed run. The run did its job: it fetched, wrote and committed
    # exactly what it was asked to. It is the DATA the gate refused, so the outcome is reported
    # (`published`, and the range) rather than raised — an operator needs to see a run that
    # completed and did not publish, which is a different thing from one that broke.
    publication = _publish(catalog, spec, result.version)
    return {
        "committed_version": result.version,
        # THIS run's rows. `result.rows` is the dataset total, which is the same number only for a
        # tier's first run — the second in-cluster lane reported 8 units done for 4 ingested files
        # because the two were conflated.
        "rows": result.rows_added,
        "dataset_rows": result.rows,
        "errors": errors,
        "status": "COMPLETE_WITH_ERRORS" if errors else "COMPLETE",
        **publication,
    }


def _publish(catalog: Any, spec: RunSpec, version: int) -> dict[str, Any]:  # noqa: ANN401 — the catalog seam
    """Publish the committed version, and report the RANGE it covers (§ D2 D-R3).

    `from_version`/`to_version` are what a consumer needs to resolve an exact row delta
    (`_row_created_at_version > from AND <= to`) without keeping a bookmark of its own.

    A catalog that cannot publish must not turn a good ingest into a failed one: the rows are
    committed and a later publish can still gate them, so the failure is REPORTED on the run rather
    than raised. Silence here would be worse than either — a run that looks published and is not.
    """
    publish = getattr(catalog, "publish", None)
    if publish is None:
        return {"published": False, "publish_error": "catalog has no publish operation"}
    try:
        body = publish(spec.project, spec.dataset, version)
    except Exception as exc:
        _log.warning("publish failed for run %s at version %s: %s", spec.run_id, version, exc)
        return {"published": False, "publish_error": str(exc)}
    return {
        "published": bool(body.get("published")),
        "from_version": body.get("from_version"),
        "to_version": body.get("to_version"),
        "publish_reason": body.get("reason"),
    }


def _catalog() -> Any:  # noqa: ANN401 — LocalCatalog or CatalogServiceClient, one seam
    from ingest.catalog_service import build_catalog

    return build_catalog(BRONZE_SCHEMA)


def lineage_emitter() -> Any:  # noqa: ANN401
    from ingest.lineage import LineageRecorder

    return LineageRecorder()
