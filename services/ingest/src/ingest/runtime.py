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

import asyncio
import contextlib
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

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
#
# `partition_key` is how this plane answers "partition at volume level / at folder level". It is a
# COLUMN, not a fragment boundary, and that choice is measured rather than preferred:
#
#   * Lance has no table-level partitioning. `lance_docs/ns_catalog/partitioning-spec.md:4` —
#     "Lance tables do not natively support partitioning, instead promoting clustering to achieve
#     similar performance benefits." `write_fragments` takes no partition key; its only splitting
#     levers are size-based.
#   * Grouping units into key-pure FRAGMENTS does not survive. Measured against pylance 9.0.0: five
#     fragments each holding one distinct key, committed, then `services/maintenance`'s default
#     `compact_files()` → ONE fragment holding all five. Compaction is opt-OUT and sweeps every
#     discovered dataset, so the merge is the default, not an edge case.
#   * A column plus a scalar index DOES survive: after that same compaction, a BITMAP index on the
#     column made `partition_key = 'x'` answer as an index-served `ScalarIndexQuery` rather than a
#     scan.
#
# NULLABLE on purpose. A source that has no meaningful grouping writes nulls rather than inventing
# one, and a run predating this column is not retroactively wrong. The value comes from the ADAPTER
# (`sources.partition_of`), never from parsing the URI in the worker — the worker resolves units by
# scheme and knows nothing about volumes or prefixes, and teaching it would re-weld the source into
# the worker, which is precisely what I1 removed.
#
# NOTE FOR EXISTING DATASETS: appending a fragment carrying this column into a dataset created
# WITHOUT it raises `OSError: Append with different schema` (measured). Bronze is reproducible from
# source by design, but an existing dataset needs an `add_columns` alter before this plane can write
# to it again.
BRONZE_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64()),
        pa.field("source_uri", pa.string()),
        blob_field("payload", nullable=False),
        pa.field("sha256", pa.string()),
        # The listing fingerprint (S3 ETag) at the moment this row was WITNESSED — distinct from
        # `sha256`, which fixes the fetched BYTES. Different jobs: the etag is identity material
        # (`identity.unit_id` folds it into `id`, so a replaced object lands as a NEW row); the
        # sha256 is fixity proof. Nullable, because token-less sources (local-dir) are snapshot
        # semantics by contract; existing tables gain the column schema-only at ensure.
        pa.field("etag", pa.string()),
        pa.field("stage", pa.string()),
        pa.field("partition_key", pa.string(), nullable=True),
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
    """Resolve a run to the location it writes — I2's "no hardcoded dataset paths".

    Keyed on ``spec.namespace``, NOT ``spec.project``, and that is the whole of this function. The two
    are different levels — a project selects the storage root, a namespace is the medallion TIER — and
    this helper was left behind when `RunSpec.namespace` became "THE ONE PLACE a project becomes a
    namespace". It kept composing the pre-namespace shape:

        dataset_uri  ->  <warehouse>/p/pages.lance          (project)
        catalog.ensure(spec.namespace, ...) -> <warehouse>/p-bronze/pages.lance   (tier)

    Production never noticed, because production does not call this — `finalize_run` goes through
    `catalog.ensure(spec.namespace, spec.dataset)` and every other writer resolves the same way. The
    only remaining caller was `test_run_chain`, which therefore staged fragments at one path and
    committed at another: the commit found no data files, the run produced no visible version, and the
    test asserted "a run must produce exactly ONE visible commit" against a path nothing had written.

    So this now derives from the same property the catalog keys on, and the two cannot diverge again.
    It stays a function rather than being deleted because it is the path-based form the LOCAL catalog
    needs (`ensure_at`), and open-coding the composition at each test site is how the drift started.
    """
    return f"{warehouse_root().rstrip('/')}/{spec.namespace}/{spec.dataset}.lance"


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


def ensure_dataset_at(spec: RunSpec) -> tuple[str, int]:
    """Create the run's dataset empty if absent; return (URI to write into, BASE VERSION). D6 step 1.

    The URI is whatever the CATALOG says it is. In-cluster that is a location the catalog vends;
    locally it is composed from the warehouse env. Either way the caller never names a path — I2's
    "no hardcoded dataset paths", which exists because two callers composing the same logical table
    from different env is how volume B overwrote volume A.

    THE VERSION IS RESOLVED HERE FOR THE SAME REASON THE LOCATION IS. It is the `read_version` the
    run's client-direct commit is built against, and re-deriving it inside `finalize` is what made
    the catalog's per-run commit dedupe unreachable: the catalog recognizes a replayed commit by
    scanning versions AFTER the presented `read_version`, and a retry that re-read the version got
    the one its own first attempt had just produced — an empty scan window, and a second Append of
    the same rows. Resolved once, carried on `DatasetHandle`, presented identically by every attempt.

    Zero for a catalog with no version door (`LocalCatalog`): that path commits through the lander,
    which reads the dataset's own current version, and never sends a `read_version` anywhere.
    """
    catalog = _catalog()
    location = str(catalog.ensure(spec.namespace, spec.dataset))
    describe_version = getattr(catalog, "describe_version", None)
    read_version = int(describe_version(spec.namespace, spec.dataset)) if describe_version is not None else 0
    return location, read_version


async def publish_chunk_units(chunk: ChunkSpec) -> int:
    """Put this chunk's units on the work queue."""
    from ingest.queue import UnitTask, WorkQueue

    queue = await WorkQueue.connect(nats_url())
    try:
        await queue.ensure_stream()
        # The partition label is computed HERE, at publish, where the run's SourceSpec is still in
        # hand — the worker only ever sees a URI and a scheme.
        from ingest.sources import SourceSpec, partition_key_for

        spec = SourceSpec(kind=chunk.kind, project=chunk.project, dataset=chunk.dataset, options=chunk.options)
        # THE UNITS, from the pointer (§2.13) or from the descriptor itself. A chunk now names a window
        # into the run's unit manifest instead of carrying its keys, which is what took the workflow's
        # payloads from O(units) to O(chunks). The inline branch is the ROLLOUT path and not dead code:
        # a chunk enqueued by the previous build is replayed by this one with its recorded input, so
        # both shapes are live until every in-flight run has drained.
        #
        # Read HERE, in activity scope, where I/O belongs — the workflow body never touches it.
        if chunk.keys:
            # Tokens are positional-parallel to keys, resolved at ENUMERATE where the listing was in
            # hand; a chunk from an older build carries none and every token degrades to None.
            tokens = list(chunk.tokens) + [None] * (len(chunk.keys) - len(chunk.tokens))
            pairs = list(zip(chunk.keys, tokens, strict=True))
        else:
            from ingest.staging import read_unit_slice

            pairs = read_unit_slice(chunk.dataset_uri, chunk.run_id, chunk.offset, chunk.count)
        tasks = [
            UnitTask(
                run_id=chunk.run_id,
                chunk_id=chunk.chunk_id,
                key=key,
                dataset_uri=chunk.dataset_uri,
                partition_key=partition_key_for(spec, key),
                token=token,
            )
            for key, token in pairs
        ]
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
    from ingest.adapters import register_builtin_sources
    from ingest.fetch import UriFetcher
    from ingest.queue import WorkQueue
    from ingest.sources import fetcher_for
    from ingest.validation import PayloadValidator
    from ingest.worker import Worker

    queue = await WorkQueue.connect(nats_url())
    try:
        await queue.ensure_stream()
        # Only the DRAIN path needs this, which is why it is here and not beside the publish-side
        # `ensure_stream` above: `park_poison` fires from the worker, awaits before its `msg.ack()`,
        # and is unwrapped — so a missing DLQ stream turns the one mechanism that stops a poison unit
        # from stalling a run INTO the stall.
        await queue.ensure_dlq_stream()
        # The RUN's sizing, resolved at accept and carried on the chunk — never re-read from env here.
        # Re-reading would let a rolling restart change fragment size under a live fan-out, so two
        # chunks of one run could write different layouts and the operator would have no record of
        # which numbers the run actually used.
        # THIS KIND'S fetcher if it registered one, else the scheme-resolved default. The chunk
        # already carries `kind` (it is what `publish_chunk_units` asks the adapter for a partition
        # key with), so selecting here puts no new source knowledge on the wire and none at all in
        # `ingest.fetch` — which resolves schemes and must never learn about sources.
        register_builtin_sources()
        worker = Worker(queue, fetcher_for(chunk.kind) or UriFetcher(), PayloadValidator(), name=chunk.chunk_id, sizing=chunk.sizing)
        outcome = await worker.drain_chunk(chunk.run_id, chunk.chunk_id, chunk.expected_units, chunk.dataset_uri)
        # BOUNDED HERE, at the first point the result becomes workflow history. The map is keyed by
        # UNIT, so a chunk whose every key is corrupt carries one entry per page — and this dict is
        # then persisted as the activity result, returned by the child, merged by the parent and fed
        # back in as `finalize`'s input. The COUNT is what an operator acts on; the per-unit reasons
        # are already durable on the queue's DLQ.
        from ingest.workflow import bound_errors

        listed, total = bound_errors(outcome.errors)
        return {**outcome.model_dump(), "errors": listed, "errors_total": total}
    finally:
        await queue.close()


#: A terminal run must not wait on a broker to finish terminating. Measured earlier in this plane: a
#: nats connect to a dead address had still not returned after 60s with `connect_timeout`,
#: `allow_reconnect=False` and `max_reconnect_attempts=0` ALL set, so `asyncio.wait_for` around the
#: whole thing is the only reliable bound. Without it this call took the ingest suite from 21s to 150s.
RELEASE_TIMEOUT_SECONDS = 5.0


async def release_run_units(run_id: str) -> int:
    """Drop whatever this run left queued. Returns the count released, 0 if it could not.

    Lives here rather than in the workflow because I3 confines the broker client to `ingest.queue` —
    the activity calls this, this calls the seam.

    NEVER RAISES, and the CONNECT is inside the guard. `release_run` already swallows its purge and
    delete failures, and the first version of this stopped there — which left `connect()` outside,
    so an unreachable broker raised `NoServersError` straight out of the terminal activity and failed
    a run that had already landed its data. A test caught it; the docstring above it had claimed
    "best-effort by construction" while the code was not.

    That is the I8 shape exactly: tidying up must never fail the thing it is tidying up after.
    """
    from ingest.queue import WorkQueue

    queue = None
    try:
        queue = await asyncio.wait_for(WorkQueue.connect(nats_url()), timeout=RELEASE_TIMEOUT_SECONDS)
        return await asyncio.wait_for(queue.release_run(run_id), timeout=RELEASE_TIMEOUT_SECONDS)
    except Exception:
        _log.warning("could not release queued units for run %s — they may remain on the stream", run_id, exc_info=True)
        return 0
    finally:
        if queue is not None:
            with contextlib.suppress(Exception):
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


def finalize_run(spec: RunSpec, fragments: list[str], errors: dict[str, str], *, read_version: int = 0) -> dict[str, Any]:
    """Commit the run's fragments as ONE version, through the lander.

    `COMPLETE_WITH_ERRORS` is a real terminal state, not a failure: a run where 3 of 10,000 pages
    were corrupt DID deliver 9,997 pages, and calling that FAILED would either discard good data or
    train operators to ignore the status field.

    `read_version` is the version resolved at `ensure_dataset` and CARRIED — it is not re-read here,
    and that is the whole of finding F12a. This activity is re-executed whenever Dapr did not durably
    record its result, so a commit that landed and then lost its pod runs again; the catalog answers
    a repeat of the same `(run_id, read_version)` with the version that run already committed, which
    it can only do while the presented base version stays put. Re-reading it moved the scan window
    past the run's own commit and the retry appended every row a second time.

    The default of 0 is the LANDER path (`LocalCatalog`), which sends no `read_version` anywhere and
    reads the dataset's current version itself. The workflow always passes the carried one.
    """
    from ingest.lander import CommitResult, Lander
    from ingest.staging import discover_staged, purge_staged

    catalog = _catalog()
    uri = catalog.ensure(spec.namespace, spec.dataset)
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
            spec.namespace,
            spec.dataset,
            all_fragments,
            # CARRIED, never re-read. See this function's docstring and `workflow.DatasetHandle`.
            read_version=read_version,
            run_id=spec.run_id,
        )
        # `row_count` from the catalog is the DATASET's total after the commit, not this run's work —
        # the same distinction the Lander path already draws, and it came straight back the moment the
        # catalog path bypassed that code: a second run against one dataset reported 8 units done for
        # 4 ingested files. Counted here from the committed FRAGMENTS, which is also the only form
        # that stays right under concurrent commits.
        added = _rows_in(all_fragments)
        result = CommitResult(dataset_uri=uri, version=version, rows=tier_rows, rows_added=added, fragments_committed=len(all_fragments))
        # The index policy the lander applies on ITS commit path — this branch bypasses the lander,
        # and every catalog-committed table shipped index-less until the explain-plan gate caught it.
        from ingest.lander import ensure_indexes_at

        ensure_indexes_at(uri)
    else:
        # The SAME carried version the catalog branch uses. This branch is dev/test-only
        # (`LocalCatalog` has no `commit`), which is the only reason F12a's fix stopped here — but
        # `finalize` is an at-least-once activity in both, so a retry re-reading the version is the
        # same defect wearing a different catalog.
        result = Lander(catalog).commit_fragments(uri, all_fragments, run_id=spec.run_id, read_version=read_version)
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


class PublishSpec(Protocol):
    """The three fields `_publish` READS off a run spec, named so a stand-in is CHECKED against them.

    `RunSpec` satisfies it, and so must any test double. Annotating the concrete model and letting a
    double through on trust is how this call came to swallow an AttributeError into its
    catalog-cannot-publish branch: the plane started addressing the catalog by namespace, the double
    still offered only `project`, and the run reported `published: false` with a plausible reason
    while nothing raised.
    """

    @property
    def namespace(self) -> str: ...

    @property
    def dataset(self) -> str: ...

    @property
    def run_id(self) -> str: ...


def _publish(catalog: Any, spec: PublishSpec, version: int) -> dict[str, Any]:  # noqa: ANN401 — the catalog seam
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
        body = publish(spec.namespace, spec.dataset, version)
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
