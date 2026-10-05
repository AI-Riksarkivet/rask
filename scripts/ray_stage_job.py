"""Ray Data stage-transform job for the EVENT-DRIVEN medallion cascade.

A medallion stage runner submits this via the Ray Jobs REST API (services/medallion/services/ray_submit.py) IN
RESPONSE TO its Dapr cascade trigger — the production-shape replacement for the in-process fake-Ray
``compute.transform_stage``. It reads the upstream Lance dataset, stamps a ``stage`` provenance column across
Ray workers, threads the row-level ``source_rowid`` provenance column (minted at the head from ``_rowid``,
carried forward — parity with the in-process path), and writes the downstream dataset at file format 2.2 with
stable row ids (create the target with
stable ids, then distributed-append — lance_ray.write_lance has no stable-row-ids param). The stage runner then reads
the written version + statistics for the OpenLineage WROTE edge, exactly as the in-process path does.

TWO paths, chosen by whether the upstream carries a blob-v2 column:
* TABULAR → the distributed lance_ray read→map_batches(stamp)→write path (Ray workers, one commit).
* MEDIA (blob-v2 present) → a pylance-native round-trip on the driver, for the DERIVERS: image
  payloads get an inline thumbnail + embedding computed here. A blob column is re-materialised via a
  ``blob_handling="all_binary"`` scan (NOT ``read_blobs`` — it drops null rows) and re-wrapped with
  ``blob_array`` before a 2.2 write. This is the SAME contract as compute.transform_stage / derivers, and by the
  same code: the derivers are IMPORTED from ``service_kit.lakehouse.media``, not inlined and
  drift-pinned (B14 — the pin was the wrong fix, and this docstring described it long after the copies
  were gone). Closes the Phase-3 gap that forced media stages onto the in-process fallback.

Consume-layer provenance (R26): the submitting stage runner hands over this run's ``LineageDoc`` as
``RASK_LINEAGE_DOCUMENT`` (the order's own field name), and every path below writes it as the ``lineage`` column (Arrow JSON → Lance JSONB) in
the SAME commit as the data — a governed row must never be readable without its provenance, and the
distributed path must not produce a dataset the in-process path would have stamped. Any upstream
``lineage`` cell is DROPPED first: it describes the parent's run, not this one. Unset/empty → no column
(the pre-R26 shape), so the job stays runnable by hand.

Env: RASK_SOURCE_URI RASK_DEST_URI RASK_STAGE [RASK_LINEAGE_DOCUMENT RASK_VERSION_FLOOR
     RASK_CARDINALITY RASK_IDEMPOTENCY_KEY RASK_RUN_ID RASK_OUTCOME_URL]  S3_ENDPOINT S3_KEY S3_SECRET [S3_REGION]
     — the last three name the run (its commit marker) and the planner's outcome door it reports to (CP-029).
     [TRACEPARENT TRACESTATE OTEL_*] — trace continuity across the Ray boundary (prod-readiness P3):
     when the submitting stage runner injected its span + OTLP config, the job runs under one root span
     parented on that trace; absent → untraced, exactly as before.
"""
# TOKEN-AUTHED CLUSTER (gate 7 / R3): with RAY_AUTH_MODE=token on the head, export
# RAY_AUTH_MODE=token + RAY_AUTH_TOKEN (kubectl get secret rask-ray-auth-token -o
# jsonpath='{.data.auth_token}' | base64 -d) before submitting. `ray job submit` /
# JobSubmissionClient then attach `Authorization: Bearer` themselves; any RAW
# requests/httpx call against the dashboard (:8265) must send that header itself.
# NEVER put the token in runtime_env.env_vars — the jobs API echoes runtime_env back.

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from collections.abc import Iterator
from typing import Any

import lance
import pyarrow as pa
from lance import blob_array, blob_field
from lance.commit import CommitConflictError

# lance_ray ships in the Ray image, NOT our services' venv — imported LAZILY (inside the tabular branch
# of main) so the deriver primitives below stay importable in the unit venv for the drift-pin test
# (tests/unit/test_ray_stage_job.py), exactly as ray_train_job keeps `lance` out of its module top.
# --- blob + deriver primitives: IMPORTED, not inlined (B14) --------------------------------------
# These were copies kept byte-identical to the services by a drift-pin test. A test comparing two
# behaviours after the fact is what B14 records as the WRONG fix: it detects divergence, it does not
# prevent it, and it only detects the cases someone thought to assert.
#
# Both drivers can import `service_kit` — this script already did, for `stamp_stage` — so the
# implementations moved there and both now call ONE function. `media` rides the optional
# `service-kit[media]` extra, which the ray-cluster image installs.
from service_kit.lakehouse import media
from service_kit.lakehouse.blobs import blob_field_names
from service_kit.lakehouse.commit_marker import CommitMarker, stamped
from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base, lance_storage_options, s3_filesystem
from service_kit.lakehouse.run_outcomes import OutcomeReport, report_outcome
from service_kit.lakehouse.stage_stamp import (
    CARDINALITIES,
    LINEAGE_COLUMN,
    ONE_TO_ONE,
    SOURCE_ROWID_COLUMN,
    STAGE_COLUMN,
    UnstableRowIdsError,
    ensure_declared_dataset_id,
)


# --- the commit marker (CP-029 D-5) ----------------------------------------------------------------
# Every lane ends on ONE markable destination commit and orders every commit it cannot mark (a delete, a schema-metadata
# stamp) before it, so "the run's marker is in the destination's history" implies "every write of the run landed".
# `write_dataset` carries the marker in its own transaction properties; a merge carries it through
# `execute_uncommitted` and a stamped `LanceDataset.commit` (measured on pylance 12.0.0, `commit_marker`'s docstring).


#: How many times a marked merge that lost a race is planned again against the newer version. The same bound
#: `MergeInsertBuilder.conflict_retries` defaults to ("Default is 10", pylance 12.0.0), so a marked merge survives the
#: contention an unmarked `.execute()` survives.
MERGE_CONFLICT_RETRIES = 10


def _properties(marker: CommitMarker | None) -> dict[str, str] | None:
    return marker.properties() if marker is not None else None


def _converge(
    to_uri: str, table: pa.Table | lance.LanceDataset, so: StorageOptions, marker: CommitMarker | None, *, retract: str | None = None, full_sync: bool = False
) -> None:
    """Merge ``table`` (a table, or a dataset the merge streams) into the destination on `id`, as ONE commit carrying ``marker``.

    ``full_sync`` deletes every destination row the source does not carry; ``retract`` deletes only those of them that
    also match the predicate (the media lane's "not written by this run", folded into its last batch's merge so the
    retraction and the marker are one commit).

    A MARKED MERGE RE-PLANS ITSELF WHEN IT LOSES A RACE. `.execute()` re-runs a merge preempted by a concurrent commit
    (`conflict_retries`); `execute_uncommitted` plus `LanceDataset.commit` does not, because the commit's own
    `max_retries` only rebases a transaction that CAN be rebased. A merge that a concurrent merge, compaction or delete
    preempted raises `lance.commit.CommitConflictError` with ``retryable=True`` (measured on pylance 12.0.0, against a
    concurrent merge and a concurrent `compact_files`), which `lance_docs/file_format.md` § Conflict Resolution defines
    as "re-execute at the application level with updated data". So the merge is planned again against the newest
    version and committed again. An incompatible conflict (``retryable=False``, a concurrent overwrite replaced the
    table) is raised unchanged: re-planning would merge into contents this run never read.
    """
    for attempt in range(MERGE_CONFLICT_RETRIES + 1):
        builder = lance.dataset(to_uri, storage_options=so).merge_insert("id").when_matched_update_all().when_not_matched_insert_all()
        if full_sync:
            builder = builder.when_not_matched_by_source_delete()
        elif retract is not None:
            builder = builder.when_not_matched_by_source_delete(retract)
        if marker is None:
            builder.execute(table)
            return
        transaction, _stats = builder.execute_uncommitted(table)
        try:
            lance.LanceDataset.commit(to_uri, stamped(transaction, marker), storage_options=so)
        except CommitConflictError as exc:
            if not exc.retryable or attempt == MERGE_CONFLICT_RETRIES:
                raise
            print(f"RAY-STAGE merge into {to_uri} lost a commit race (attempt {attempt + 1}); planning it again: {exc}")
            continue
        return


def _storage_options() -> StorageOptions:
    """The estate's builder, not a hand-rolled copy — the B14 ruling this file already records.

    Two things the copy got wrong, and the second is the one that matters. It spelled the credential
    keys BARE (`access_key_id`), where object_store BLENDS the ambient `AWS_*` environment with bare
    keys and signs with a pair belonging to neither identity — measured in-cluster 2026-09-03, a
    `403 SignatureDoesNotMatch` that reads as an expired credential, and one that NO test can catch
    because no test process carries an ambient AWS_* environment. And it had no `session_token` field
    at all, so this lane could not hold an STS credential even once one was vended to it: a triple
    arriving at a builder that accepts a pair signs as the wrong identity or not at all.
    """
    return lance_storage_options(
        os.environ["S3_ENDPOINT"],
        os.environ["S3_KEY"],
        os.environ["S3_SECRET"],
        os.environ.get("S3_REGION", "us-east-1"),
    )


def _reset_dataset(to_uri: str, so: StorageOptions) -> None:
    """Delete any existing dataset at ``to_uri`` so the create-with-stable-ids below is truly fresh.

    ``enable_stable_row_ids`` is a create-time-only property: ``mode="overwrite"`` on a dataset that already
    exists WITHOUT stable ids (e.g. one a prior in-process run created) does NOT flip it on. The cascade uses
    overwrite semantics (each run's output IS the whole dataset), so clearing the dir first is correct here.
    """
    # `s3_filesystem`, not a hand-built one: it derives the scheme from the endpoint (a hardcoded
    # `http` once silently downgraded a secured connection) and it FORWARDS the session token. pyarrow
    # falls back to the default credential chain for anything it was not given, so a half-forwarded
    # vended credential signs with the pod's own role — broader rights than the catalog scoped, not
    # narrower. Dropping it fails OPEN, which is why this belongs to one builder and not to a copy.
    fs = s3_filesystem(so)
    with contextlib.suppress(OSError):
        fs.delete_dir_contents(to_uri.removeprefix("s3://"), missing_dir_ok=True)


def _reset_if_legacy(to_uri: str, so: StorageOptions) -> None:
    """Clear the target ONLY if a legacy dataset (created without stable ids) exists there.

    ``enable_stable_row_ids`` is create-time-only, so overwrite alone won't flip it on a pre-existing no-id
    dataset; a dataset that already has stable ids keeps them under overwrite, so the raw dir-wipe (+ its
    concurrency hazard) is a one-time migration, skipped on the common already-stable path.
    """
    needs_reset = False
    with contextlib.suppress(Exception):
        needs_reset = not lance.dataset(to_uri, storage_options=so).has_stable_row_ids
    if needs_reset:
        _reset_dataset(to_uri, so)


#: Lance's reserved row-identity metacolumn, as the cascade head's join key (`_retract_deleted`).
#: Read, never written — the value advances on the next overwrite, so persisting it records an id that
#: will not be true.
_ROWID = "_rowid"


def _stamp_stage(table: pa.Table, stage: str, lineage: str = "", dataset_id: str = "", *, stable_row_ids: bool) -> pa.Table:
    """The per-stage provenance stamp — delegated to the ONE implementation both drivers share.

    This was a hand-maintained mirror of the medallion's copy and it had already drifted: on a
    re-stamp it dropped `stage` and re-appended it at the end while the in-process driver replaced it
    in place, so a silver table's column ORDER — and therefore its dataset schema, since
    `write_dataset(mode="overwrite")` takes the table's — depended on which compute path wrote it.

    It is now the ONLY thing in this job that decides where a provenance column sits: the media lane
    stamps through it, the distributed lane's blocks come out of it, and the schema the distributed
    lane creates its destination with is derived from it (:func:`_target_schema`).

    ``stable_row_ids`` is the upstream's `has_stable_row_ids`: the stamp refuses to mint `source_rowid`
    from an upstream without them (`stage_stamp.carry_source_rowid`).
    """
    from service_kit.lakehouse.stage_stamp import stamp_stage

    return stamp_stage(table, stage=stage, stable_row_ids=stable_row_ids, lineage=lineage, dataset_id=dataset_id)


def _target_schema(upstream: lance.LanceDataset, stage: str, lineage: str, dataset_id: str) -> pa.Schema:
    """The schema the distributed lane must create its destination with: the transform's OWN output.

    Derived by running the real stamp over a zero-row slice of the real upstream — not rebuilt from a
    field list. That distinction is the 2026-08-30 defect, which broke every tabular cascade at gold:
    this schema was assembled by dropping `stage`/`lineage` from the upstream and appending them
    back, while the blocks came from :func:`_stamp_stage`, which re-stamps IN PLACE and so keeps the
    upstream's positions. Over the producer's bronze (`id, payload, stage`) they differ from silver
    on — `[…, stage, source_rowid, lineage]` emitted against `[…, source_rowid, stage, lineage]`
    created — and `lance_ray` casts each block to the destination schema BY POSITION
    (`lance_ray/pandas.py::pd_to_arrow` → `df.cast(schema)`), so the append died with
    `LanceError(Arrow): … field names are not matching`. Same columns, one transposed pair, no run.

    Two constructions of one schema can only ever agree by luck, which is the class `stage_stamp`'s
    own module docstring records for the two drivers. One function answers for both sides here.
    """
    return _stamp_stage(upstream.schema.empty_table(), stage, lineage, dataset_id, stable_row_ids=upstream.has_stable_row_ids).schema


#: How many rows the media lane holds in the driver at once.
#:
#: The MEDIA branch runs its round-trip on the driver, and that used to happen ALL AT ONCE: one `to_table()` over every blob
#: payload, a second full copy as Python bytes from `to_pylist()`, and two more as the thumbnail and
#: embedding lists. Peak RSS scaled with the dataset, so the cascade had an OOM ceiling nothing
#: announced (ray-project's own `patterns/generators.rst`: yield in chunks rather than materialise).
#: Bounded, the driver holds ~4x one batch of payloads; at the media lane's ~1.8 MB page images 128
#: rows is a few hundred MB. `RASK_STAGE_MEDIA_BATCH_ROWS` tunes it for other payload sizes.
MEDIA_BATCH_ROWS = int(os.environ.get("RASK_STAGE_MEDIA_BATCH_ROWS", "128"))

#: MEASURED 2026-09-20 in the deployed `ray-lance` image (pylance 11.0.0, pyarrow 25.0.0,
#: lance-ray 0.5.0): `write_lance(..., data_storage_version="2.2")` PRESERVES blob-v2 —
#: `extension<lance.blob.v2<BlobType>>` round-trips intact. Without that argument it writes V2_1 and
#: Lance refuses the column outright ("Blob v2 requires file version >= 2.2"), which is what the
#: "strips blob typing" reading came from. So blob typing is not what keeps MEDIA on the driver; the
#: derivers are ([[LH-085]]).


def _derivable_blob_column(ds: Any, blob_cols: list[str]) -> str | None:
    """Which blob column (if any) gets thumbnail + embedding — decided ONCE, before the stream.

    The unbatched form could decide this per run because it held every payload; a streamed one
    cannot decide per batch, because the derived columns are part of the SCHEMA and a later batch
    that disagreed with the first would fail the append. So the probe scans forward for the first
    non-null payload — the same "first non-null decides" contract as before, and the same
    first-match-wins rule as `derivers._DERIVERS` — and the answer governs every batch.

    A column of entirely null payloads yields None, which is the honest answer: there is nothing to
    decide from, and null artifacts on a null payload are what the unbatched form produced anyway.

    A tier that ALREADY carries the artifacts derives nothing — the same first line as
    ``derivers.derive_artifacts`` ("skips when the upstream already carries the artifact columns; a
    later stage carries them forward rather than re-deriving"), which this driver was missing. Without
    it the carried column and the freshly appended one collided and the write died
    ``LanceError(Schema): Duplicate field name "thumbnail"``, so the media lane could do exactly one
    hop: bronze→silver worked and silver→gold could not.
    """
    if any(name in ds.schema.names for name in media.ARTIFACT_COLUMNS):
        return None
    for name in blob_cols:
        scanner = ds.scanner(columns=[name], blob_handling="all_binary", batch_size=MEDIA_BATCH_ROWS)
        for batch in scanner.to_batches():
            probe = next((p for p in batch.column(name).to_pylist() if p is not None), None)
            if probe is None:
                continue
            return name if media.is_image(probe) else None
    return None


#: A run id the retraction predicate can be built from safely. The document is the platform's own, but
#: the value is INTERPOLATED into a filter string, so anything outside this alphabet is refused rather
#: than escaped — a predicate that cannot be built correctly must not be built at all.
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _run_id_of(lineage: str) -> str | None:
    """This run's `run_id` from its lineage document, or ``None`` when there is not a usable one.

    ``None`` means the caller must not retract: an unwired lane (`lineage` empty), a document that is
    not JSON, or an id that cannot be interpolated into a filter. Every one of those is "we cannot tell
    which rows are ours", and the only safe answer to that is to leave the tier alone.
    """
    if not lineage:
        return None
    try:
        run = json.loads(lineage).get("run_id")
    except (ValueError, AttributeError):
        return None
    return run if isinstance(run, str) and _SAFE_RUN_ID.match(run) else None


def _media_transform(
    from_uri: str, to_uri: str, so: StorageOptions, *, stage: str, lineage: str = "", dataset_id: str = "", marker: CommitMarker | None = None
) -> None:
    """The MEDIA path: pylance-native blob round-trip + inline image derivation, then a 2.2 stable-id write.

    Same contract as compute.transform_stage + derivers.derive_artifacts: re-materialise each blob column
    as bytes and re-wrap with ``blob_array`` (lance_ray's write would demote it to plain binary), stamp
    the provenance columns through the shared ``stamp_stage``, and for a blob column whose first
    non-null payload decodes as an image, append an inline ``thumbnail`` (PNG) + ``embedding``
    (fixed-size floats). Non-image blobs carry through untouched, and an upstream that ALREADY carries
    the artifacts carries them forward rather than deriving a second pair (see
    :func:`_derivable_blob_column`). ``lineage`` re-stamps the consume-layer provenance column (R26) in
    this same write.

    NULL-SAFE BY CONSTRUCTION (R27), mirroring compute._carry_forward: ONE
    ``scanner(blob_handling="all_binary")`` scan carries tabular AND blob columns row-aligned with the
    nulls intact. ``read_blobs``/``take_blobs`` DROP null rows (measured, pylance 9.0.0 —
    docs/architecture/lance-blob-v2-findings.md), so the previous ``to_table()`` + positional
    ``read_blobs`` pair failed the whole stage on ONE un-harvested page. A null payload now carries
    forward as a null blob with null artifacts.

    STREAMED, in ``MEDIA_BATCH_ROWS`` slices. The scan, the derivation and the write are one pass per
    batch, so what the driver holds is bounded by the batch rather than by the run.

    A RERUN MERGES AND RETRACTS ONCE, and that is what preserves row identity. The tier ABOVE stores
    this tier's stable ``_rowid`` as its ``source_rowid``, so re-creating the target re-mints every
    parent id it holds — measured on the live estate as silver's 8 ``source_rowid`` values naming bronze
    rows that no longer existed, 8 of 8. A plain per-batch ``when_not_matched_by_source_delete`` would
    delete the rows earlier batches just wrote, so the retraction is CONDITIONAL and rides the LAST
    batch's merge alone: it deletes a destination row the last batch does not carry only when that row's
    own ``lineage`` document names another run. Rows earlier batches wrote name this run and stay. That
    is only expressible because ``lineage`` is JSONB and therefore filterable in place, and folding it
    into the last merge makes the retraction and the run's commit marker ONE commit (measured on
    pylance 12.0.0: the predicate deleted exactly the other run's row the source did not carry).

    The first write of a target that does not exist still CREATES it, because ``enable_stable_row_ids``
    is create-time-only and a merge cannot turn it on. The last batch is held back one step (one batch
    of lookahead) so its write is the one that carries the marker.
    """
    ds = lance.dataset(from_uri, storage_options=so)
    blob_cols = blob_field_names(ds.schema)
    carried = [f.name for f in ds.schema if f.name not in (STAGE_COLUMN, LINEAGE_COLUMN)]
    derive_from = _derivable_blob_column(ds, blob_cols)

    # with_row_id so the head can mint source_rowid from the SAME aligned scan (a carried source_rowid is a
    # plain column already in this read); mirrors compute._carry_forward's blob path.
    scanner = ds.scanner(columns=carried, blob_handling="all_binary", with_row_id=True, batch_size=MEDIA_BATCH_ROWS)

    run = _run_id_of(lineage)
    # THE RETRACTION'S PREDICATE: every row this run wrote carries this run's id in its own `lineage` document, so
    # "not written by this run" is a filter rather than a set the driver has to hold. None when the lane is unwired
    # (`lineage` empty, so `run` is None): with nothing identifying this run, the predicate would match every row and
    # the retraction would empty the tier — absent provenance must fail SAFE, not destructively.
    retract = f"json_get_string({LINEAGE_COLUMN}, 'run_id') != '{run}'" if run else None
    fresh = not _dataset_exists(to_uri, so)
    written = 0
    held: pa.Table | None = None

    def land(out: pa.Table, *, last: bool) -> None:
        nonlocal fresh
        mark = marker if last else None
        if fresh:
            # CREATE, not merge: `enable_stable_row_ids` is create-time-only, so a target that does not
            # exist yet has to be written into being before anything can merge into it. A created target
            # holds only this run's rows, so it has nothing to retract.
            lance.write_dataset(
                out,
                to_uri,
                mode="overwrite",
                storage_options=so,
                data_storage_version="2.2",
                enable_stable_row_ids=True,
                transaction_properties=_properties(mark),
            )
            fresh = False
        else:
            _converge(to_uri, out, so, mark, retract=retract if last else None)

    for batch in scanner.to_batches():
        out = _media_batch(
            pa.Table.from_batches([batch]), blob_cols, derive_from, stage=stage, lineage=lineage, dataset_id=dataset_id, stable_row_ids=ds.has_stable_row_ids
        )
        if held is not None:
            land(held, last=False)
        held = out
        written += out.num_rows
    if held is not None:
        land(held, last=True)

    if written == 0:
        # An empty source still has to produce the target — an absent dataset is not the same answer
        # as an empty one, and the tier's readers open it either way.
        empty = _media_batch(
            ds.scanner(columns=carried, blob_handling="all_binary", with_row_id=True, limit=0).to_table(),
            blob_cols,
            derive_from,
            stage=stage,
            lineage=lineage,
            dataset_id=dataset_id,
            stable_row_ids=ds.has_stable_row_ids,
        )
        lance.write_dataset(
            empty,
            to_uri,
            mode="overwrite",
            storage_options=so,
            data_storage_version="2.2",
            enable_stable_row_ids=True,
            transaction_properties=_properties(marker),
        )


def _media_batch(
    aligned: pa.Table, blob_cols: list[str], derive_from: str | None, *, stage: str, lineage: str, stable_row_ids: bool, dataset_id: str = ""
) -> pa.Table:
    """One slice: re-wrap its blobs, stamp its provenance, derive its artifacts.

    Every batch takes the same branches and therefore produces the same schema, which is what lets
    the caller append them into one dataset.

    THE PROVENANCE STAMP IS :func:`_stamp_stage`, not a copy of it. This function used to append
    `source_rowid`, `stage` and `lineage` itself — a third construction of the same three columns in
    a job that already had two, and one that appended `stage` UNCONDITIONALLY (harmless only because
    the scan above excludes it, which nothing stated). Handing the aligned batch — `_rowid` and all —
    to the shared stamp gets the identical schema from the one function that owns the question.
    """
    columns: dict = {}
    fields: list[pa.Field] = []
    for name in aligned.schema.names:
        if name in blob_cols:
            fields.append(blob_field(name))
            columns[name] = blob_array(aligned.column(name).to_pylist())
        else:
            # `_rowid` included deliberately: the stamp mints `source_rowid` from it at the cascade
            # head and drops it (it is Lance's reserved metacolumn and is never persisted).
            fields.append(aligned.schema.field(name))
            columns[name] = aligned.column(name)
    out = _stamp_stage(pa.table(columns, schema=pa.schema(fields)), stage, lineage, dataset_id, stable_row_ids=stable_row_ids)

    # Row-wise, image payloads only — a payload past the header probe that fails full decode raises,
    # FAILing the run; a NULL payload (absent bytes, not bad bytes) keeps its row with null artifacts.
    if derive_from is not None:
        payloads = aligned.column(derive_from).to_pylist()
        out = out.append_column(
            pa.field(media.THUMBNAIL_COLUMN, pa.large_binary()),
            pa.array([None if p is None else media.derive_thumbnail(p) for p in payloads], pa.large_binary()),
        )
        out = out.append_column(
            pa.field(media.EMBEDDING_COLUMN, pa.list_(pa.float32(), media.EMBEDDING_DIMS)),
            pa.array([None if p is None else media.derive_embedding(p) for p in payloads], type=pa.list_(pa.float32(), media.EMBEDDING_DIMS)),
        )
    return out


# --- trace continuity across the Ray boundary (prod-readiness P3) ---------------------------------------
# Byte-identical in ray_stage_job.py / ray_train_job.py (the self-contained-job convention — no services/
# imports), pinned equal by tests/unit/test_ray_trace_continuity.py so the two copies can never drift.


def _extract_trace_parent() -> Any:
    """The submitter-injected W3C trace context, or ``None`` to run untraced.

    The submitting service (services/medallion/services/ray_submit.py) injects its active span as a
    TRACEPARENT env var in the job's runtime_env. Absent, malformed, or opentelemetry unimportable
    (the ray image ships the SDK, but a telemetry regression must never kill the job) → ``None`` and
    the job runs exactly as before — the trace is only ever continued, never fabricated.
    """
    traceparent = os.environ.get("TRACEPARENT", "")
    if not traceparent:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        carrier = {"traceparent": traceparent}
        if tracestate := os.environ.get("TRACESTATE", ""):
            carrier["tracestate"] = tracestate
        parent = TraceContextTextMapPropagator().extract(carrier)
        if not trace.get_current_span(parent).get_span_context().is_valid:
            return None  # garbage traceparent — extract() yielded no usable span context
        return parent
    except Exception as exc:
        print(f"trace context extraction failed: {exc}", file=sys.stderr)
        return None


@contextlib.contextmanager
def _traced_root(name: str, attributes: dict[str, str], *, span_processor: Any = None) -> Iterator[None]:
    """Run the job under one root span parented on the submitter's trace (continuity, not fabrication).

    Only when the submitter handed over a valid TRACEPARENT *and* an OTLP endpoint is configured does
    the job build a TracerProvider, start ``name`` as a child of the extracted context, and force-flush
    + shut down inline before exit (short-lived process — the same build→flush→shutdown shape as the
    train job's emit_metrics). Any missing piece → the work still runs, just untraced. ``span_processor``
    is injectable so a test can capture spans without a real export; an injected processor is the
    caller's to collect (no flush/shutdown here).
    """
    own_processor = span_processor is None
    if own_processor and not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        yield  # nowhere to export — same no-op contract as emit_metrics
        return
    parent = _extract_trace_parent()
    if parent is None:
        yield
        return
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        if own_processor:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            span_processor = BatchSpanProcessor(OTLPSpanExporter())
        resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME") or "ray-job"})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(span_processor)
        tracer = provider.get_tracer("lance.ray_jobs")
    except Exception as exc:
        print(f"trace continuation unavailable: {exc}", file=sys.stderr)
        yield
        return
    try:
        with tracer.start_as_current_span(name, context=parent, attributes=attributes) as span:
            try:
                yield
            except BaseException as exc:
                # The SDK's use_span records only Exception subclasses — a SystemExit (the jobs' own
                # verification-failure exit) would otherwise export a green UNSET span for a failed job.
                if not isinstance(exc, Exception):
                    from opentelemetry.trace import Status, StatusCode

                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
    finally:
        if own_processor:
            with contextlib.suppress(Exception):
                provider.force_flush()
                provider.shutdown()


def main() -> None:
    so = _storage_options()
    # THE PLATFORM'S OWN VOCABULARY — `WorkOrder.to_env()`, which is documented as "the ONE
    # serialization, so no adapter hand-rolls it". Reading these names is what lets ANY executor
    # submit this job: the in-process lane, the dashboard Jobs API and a KubeRay `RayJob` CR all
    # serialize the same order, so none of them needs a private translation table. A job reading a
    # name no order supplies binds it to the empty string, and an empty source URI is not a crash —
    # it is a run that scans nothing, writes nothing and reports success. Pinned by
    # `tests/unit/test_the_submitter_and_the_job_agree_on_the_wire.py`, which compares what an order
    # emits against what this file reads.
    from_uri, to_uri, stage = os.environ["RASK_SOURCE_URI"], os.environ["RASK_DEST_URI"], os.environ["RASK_STAGE"]
    lineage = os.environ.get("RASK_LINEAGE_DOCUMENT", "")  # this run's consume-layer provenance document (R26)
    # THE DELTA BOUNDARY. An order OMITS the floor when there is none rather than blanking it, because
    # a consumer reads a missing floor as "full scan" and `""` would be a different claim — and both
    # spellings arrive here as absent, which is the same answer. A first stage has no boundary to be
    # incremental against.
    raw_base = os.environ.get("RASK_VERSION_FLOOR", "").strip()
    base_version = int(raw_base) if raw_base else None
    # The lane's declared row cardinality. Absent means 1:1, which is what every default stage runner is.
    cardinality = os.environ.get("RASK_CARDINALITY", "").strip() or ONE_TO_ONE
    # THE DESTINATION'S canonical catalog name, stamped onto the schema this run writes. The order has
    # always carried it and this job never read it, so every derived tier inherited its UPSTREAM's name
    # through schema metadata — silver's compactions and silver's per-dataset FAIL events filed against
    # bronze's node. Absent means unwired, and the stamp then DROPS the inherited one rather than
    # publishing a name that describes another dataset.
    dataset_id = os.environ.get("RASK_DEST_TABLE", "").strip()

    # Continue the submitting stage runner's trace (P3): the whole stage transform runs as one child span of
    # the stage runner's medallion.transform span; without a handed-over context it runs exactly as before.
    # THE RUN'S ONE NAME, and where it reports (CP-029). The order's idempotency key is the plan's action id: the job
    # stamps it on its last commit (the commit marker) and reports its own terminal to the outcome door the order
    # names. An order with no key marks nothing; one with no door reports nothing, and the plan's sweep resolves it.
    action_id = os.environ.get("RASK_IDEMPOTENCY_KEY", "").strip()
    marker = CommitMarker(action_id=action_id, run_id=os.environ.get("RASK_RUN_ID", "").strip()) if action_id else None
    outcome_url = os.environ.get("RASK_OUTCOME_URL", "").strip()
    try:
        with _traced_root("ray.stage_job", {"lance.medallion.stage": stage}):
            version = _run_stage(
                from_uri, to_uri, stage, so, lineage=lineage, base_version=base_version, cardinality=cardinality, dataset_id=dataset_id, marker=marker
            )
    except BaseException as exc:
        # A run that committed and THEN failed (a contract check after the write) still reports failed: the planner
        # reads the destination's history for the marker, so the version it committed is recorded on the FAIL.
        if outcome_url:
            report_outcome(outcome_url, OutcomeReport(status="failed", error=f"{type(exc).__name__}: {exc}"[:4000]))
        raise
    if outcome_url:
        report_outcome(outcome_url, OutcomeReport(status="succeeded", committed_version=version))


def _assert_stage_contract(*, rows_in: int, rows_out: int, cardinality: str, parentless: int) -> None:
    """What a stage owes its tier, checked after the write.

    THIS REPLACED A ROW-COUNT EQUALITY, and the replacement is a tightening rather than a loosening.
    The old check was ``out.count_rows() != upstream.count_rows()``, which has two problems. It
    FORBIDS a shape the lakehouse is supposed to support — one row becoming many is what a video
    landing as frames, or a recording as speaker turns, actually is — and it is unstatable at all once
    a run processes a DELTA, because the destination legitimately holds rows this run never read.

    And it was never really about counting. What it protected is that no row arrives in a governed
    tier without a parent, and equal counts are only a proxy for that: a transform that swapped two
    rows for two unrelated ones passed the old check and fails this one.

    So provenance is asserted ALWAYS, for every cardinality and for delta and full runs alike, and the
    count is asserted only where a lane has DECLARED that its count should hold.

    An unknown cardinality is refused rather than defaulted. A typo in a declared lane must not buy
    the loosest contract by falling through — that is how a string-typed policy silently stops
    enforcing anything.
    """
    if cardinality not in CARDINALITIES:
        raise SystemExit(f"unknown stage cardinality {cardinality!r}; declare one of {sorted(CARDINALITIES)}")
    if parentless:
        raise SystemExit(f"stage transform produced {parentless} row(s) with no parent: {SOURCE_ROWID_COLUMN} is null")
    if cardinality == ONE_TO_ONE and rows_out != rows_in:
        raise SystemExit(f"stage transform produced wrong row count: {rows_out} out for {rows_in} in, on a {cardinality} lane")


def _delta_filter(base_version: int | None) -> str | None:
    """The change-data-feed predicate for a backfill, or `None` for a full run.

    ONE COLUMN ANSWERS BOTH HALVES of "what changed since N", and that is a measured property of
    Lance rather than a simplification: a row that has never been updated carries
    ``_row_last_updated_at_version`` equal to its CREATION version (measured 2026-09-11 on a real
    dataset — three rows created at v1, one appended at v2, one updated at v3, whose last-updated
    values read `[1, 1, 2, 3]`). So `> N` selects the inserted and the updated rows together, and
    asking `_row_created_at_version` alone — which is the feed's INSERTED predicate,
    `lance_docs/file_format.md:4277-4285` — silently drops every in-place correction.

    The catalog's feed keeps the two kinds APART (`catalog.services.changes.change_filter`) because a
    consumer applies an insert and an update differently. This lane does not: it hands whatever it
    selects to one `merge_insert` on `id`, which inserts or updates per row, so splitting the window
    here would be a branch with a single body.

    A SWEEP DOES NOT WIDEN IT. Compaction rewrites files, and if it restamped the column every delta
    run after a maintenance pass would re-derive the whole tier; measured 2026-09-11, `compact_files`
    (4 fragments → 1) and `cleanup_old_versions` leave both version columns byte-identical.

    Both columns require ``enable_stable_row_ids`` AT CREATION — setting it later is a silent no-op —
    which is why the catalog's creation contract enforces it and why every write in this file passes
    it. `None` means "everything": a first run has no boundary to be incremental against, and
    filtering against version 0 would be the same answer at more cost.
    """
    return None if base_version is None else f"_row_last_updated_at_version > {base_version}"


def _mergeable(to_uri: str, so: StorageOptions) -> bool:
    """Can this destination take a delta, or must the run rebuild it whole?

    A destination that does not exist yet, or one written before stable row ids, cannot accept a
    merge — and `_reset_if_legacy` would WIPE the legacy one. Writing only a delta into a table that
    was just wiped is silent data loss, so a delta run over such a destination degrades to a full run
    instead. It becomes mergeable on the next run, because the full run creates it with stable ids.
    """
    with contextlib.suppress(Exception):
        return bool(lance.dataset(to_uri, storage_options=so).has_stable_row_ids)
    return False


def _dataset_exists(to_uri: str, so: StorageOptions) -> bool:
    """Whether `to_uri` already holds a dataset — the create-vs-merge question.

    A read, not a stat: an object store has no directories. Mirrors `compute._dataset_exists`, which is
    the in-process half of the same decision; the two lanes must answer it identically or a tier gets
    created by one and merged by the other with different guarantees.
    """
    try:
        lance.dataset(to_uri, storage_options=so)
    # Absent, unreadable, or not a dataset: all three mean "create". (No `noqa` — `BLE001` is not
    # enabled for `scripts/`, so the directive was dead and RUF100 said so.)
    except Exception:
        return False
    return True


def _merge_into(to_uri: str, table: pa.Table, so: StorageOptions, marker: CommitMarker | None = None) -> None:
    """Converge this run's rows into the destination on the tier's key, as the run's marked commit.

    `merge_insert`, never `append`: Dapr delivers at least once, so a redelivered publication event
    WILL re-run this stage over the same delta, and an append would double every row of it with
    nothing downstream noticing. `id` is a tier-contract column (`TIER_COLUMNS`), not a workload
    assumption, so merging on it is the platform's to do.
    """
    _converge(to_uri, table, so, marker)


#: Where a distributed run stages its output before the merge that lands it.
#:
#: UNDER THE DESTINATION'S OWN PREFIX on purpose: the staging set inherits the credential and bucket
#: policy that already reach the tier, where a sibling path would need its own grant and a shared
#: scratch bucket would put one tenant's rows somewhere another tenant's credential reaches.
#:
#: NAMED IN the maintenance walk's `_CONTROL_PREFIXES`, because a staging set is a real Lance dataset
#: while it exists and would otherwise be discovered, compacted and counted among the governed tables.
#: Once the destination exists the set is already unreachable — the walk descends a dataset root's
#: children only into `tree/` — but on the run that CREATES the destination the parent is still a plain
#: directory, and a crash inside that window leaves it findable. The name carries one underscore, so the
#: walk's `__`-prefix rule does not cover it and the explicit entry is what does.
_STAGING_DIR = "_staging"


def _drop_staged(staged_uri: str, so: StorageOptions) -> None:
    """Remove a staging set, best-effort — the landing has already happened when this runs.

    NEVER RAISES. This runs after the landing has already committed, so a propagating error would turn a
    completed, correctly-landed stage into a reported failure and invite a re-run of finished work.

    NEVER SILENT EITHER. Swallowing without a word leaves an orphaned Lance dataset under the
    destination that nothing reports and nobody goes looking for — the `_staging` control prefix keeps
    the maintenance walk off it, which is right for a live run and means an abandoned one is invisible.
    The line names the path so an operator can remove it.
    """
    try:
        fs, base = fs_and_base(staged_uri, so)
        fs.delete_dir(base)
    # Broad on purpose: the landing already succeeded, so no failure shape here may undo it.
    except Exception as exc:
        print(f"RAY-STAGE WARN staging set left behind at {staged_uri}: {type(exc).__name__}: {exc}")


#: How many keys go into one `IN (...)` predicate.
#:
#: The retraction reads and deletes the SMALL side — the rows Lance recorded as deleted — but a caller
#: may delete a million rows in one version, so every predicate built from that set is chunked rather
#: than sized by whatever the deletion happened to be.
_RETRACT_CHUNK = 1000


def _in_chunks(column: str, keys: list[int]) -> Iterator[str]:
    """`column IN (...)` predicates over ``keys``, `_RETRACT_CHUNK` keys each."""
    for start in range(0, len(keys), _RETRACT_CHUNK):
        yield f"{column} IN ({', '.join(str(key) for key in keys[start : start + _RETRACT_CHUNK])})"


def _retract_deleted(upstream: lance.LanceDataset, base_version: int, to_uri: str, so: StorageOptions) -> int:
    """Drop tier rows whose upstream row was deleted since ``base_version``, and answer how many.

    WHY THE DELTA LANE NEEDS THIS AT ALL: a full run converges with
    `when_not_matched_by_source_delete`, so the run's output IS the whole tier and a row it no longer
    produces is retracted. A delta's source is by construction only what changed, so the same clause
    would delete everything the delta did not carry.

    THE DELETED SET IS LANCE'S OWN RECORD: `upstream.delta(begin_version=base).get_deleted_row_ids()`
    reads the window's transactions and names the stable `_rowid`s deleted in it (measured on pylance
    12.0.0: one `delete("id = 2")` answers `[1]`). A compaction deletes no row — it rewrites fragments
    and keeps every stable id — so a window spanning one answers `[]` and retracts nothing (measured on
    12.0.0, a Rewrite window). The cost is sized by the deletion, never by either tier: no key column is
    read whole on either side.

    THE JOIN IS ROOT PROVENANCE. `stage_stamp.carry_source_rowid` KEEPS `source_rowid` rather than
    re-minting it per hop, so `gold.source_rowid == silver.source_rowid == bronze._rowid`. At the
    cascade head the deleted `_rowid`s ARE the keys; deeper in, each is mapped to its `source_rowid`
    through the upstream at ``base_version``, where the deleted rows still exist (a `_rowid IN (...)`
    filter, which Lance answers from its row-id index).

    A `1:N` lane deeper in shares one root key between siblings, so a key is retracted only when no
    upstream row still carries it: deleting one of several children must not drop the survivors'
    descendants. That check filters on the candidate keys, not on the whole column.

    Raises:
        UnstableRowIdsError: the upstream has no stable row ids. Its `_rowid` is a physical address
            that a compaction rewrites and its deleted-row record does not exist
            (`lance_docs/file_format.md:4011-4015`), so neither side of this join means anything.
    """
    if not upstream.has_stable_row_ids:
        raise UnstableRowIdsError(f"{upstream.uri} was created without stable row ids, so a delta run cannot follow its deletions; rebuild it with a full run")
    destination = lance.dataset(to_uri, storage_options=so)
    if SOURCE_ROWID_COLUMN not in destination.schema.names:
        return 0
    deleted = sorted(
        key
        for batch in upstream.delta(begin_version=base_version, end_version=upstream.version).get_deleted_row_ids()
        for key in batch.column(_ROWID).to_pylist()
    )
    if not deleted:
        return 0
    if SOURCE_ROWID_COLUMN in upstream.schema.names:
        before = upstream.checkout_version(base_version)
        candidates = sorted(
            {
                key
                for where in _in_chunks(_ROWID, deleted)
                for key in before.to_table(columns=[SOURCE_ROWID_COLUMN], filter=where).column(SOURCE_ROWID_COLUMN).to_pylist()
                if key is not None
            }
        )
        surviving = {
            key
            for where in _in_chunks(SOURCE_ROWID_COLUMN, candidates)
            for key in upstream.to_table(columns=[SOURCE_ROWID_COLUMN], filter=where).column(SOURCE_ROWID_COLUMN).to_pylist()
        }
        dead = [key for key in candidates if key not in surviving]
    else:
        dead = deleted
    for where in _in_chunks(SOURCE_ROWID_COLUMN, dead):
        destination.delete(where)
    return len(dead)


class StagedOutputMissingError(RuntimeError):
    """The distributed write left nothing at the staging path — not the same as producing no rows."""


class StagedOutputEmptyError(RuntimeError):
    """The distributed write produced ZERO rows, which a full-sync merge would read as "delete the tier"."""


def _land_staged(to_uri: str, staged_uri: str, so: StorageOptions, marker: CommitMarker | None = None) -> int:
    """Converge a STAGED distributed output into the destination, preserving the tier's row identity.

    WHY A STAGING DATASET AT ALL. `lance_ray.write_lance` offers only create/append/overwrite — there is
    no distributed merge — and `enable_stable_row_ids` is create-time-only, so the distributed fragments
    have to land somewhere before anything can merge them. The previous shape wrote an EMPTY table with
    `mode="overwrite"` and appended into it, which re-minted `_rowid` for the whole tier every run; the
    tier above resolves its `source_rowid` against exactly those values. An append cannot be made to
    preserve identity — `lance_docs/file_format.md:3998` assigns new rows ids "sequentially starting
    from `next_row_id`", while only an update remaps one to the same id (`:4025`).

    FULL SYNC, so the semantics the stage always had survive: the run's output IS the whole tier, and a
    row it no longer produces is retracted (`when_not_matched_by_source_delete`).

    THAT CLAUSE IS ALSO WHY THE TWO REFUSALS EXIST, and they are not defensive habit. Against a source
    of zero rows it matches EVERY row in the destination and empties the tier — so a Ray stage that read
    an empty upstream, filtered everything out, or half-failed would silently destroy the data it was
    re-deriving. The estate already refuses this one lane over (the media retraction runs only
    `if written and run`, "absent provenance must fail SAFE, not destructively"); the sibling merge in
    `services/medallion/services/compute.py` carries no such guard, which is exactly how copying that
    call site would import the hazard. The two cases are separate errors because they mean different
    things to whoever is debugging a lost stage: nothing was WRITTEN, versus nothing was PRODUCED.

    Returns the row count the destination holds afterwards.
    """
    if not _dataset_exists(staged_uri, so):
        raise StagedOutputMissingError(
            f"the distributed write left no dataset at {staged_uri!r} — refusing to land it, because an "
            f"ABSENT staged output read as an empty one would retract every row of {to_uri!r}"
        )
    staged = lance.dataset(staged_uri, storage_options=so)
    if staged.count_rows() == 0:
        raise StagedOutputEmptyError(
            f"the distributed write produced ZERO rows into {staged_uri!r} — refusing to land it, because a "
            f"full-sync merge of an empty source retracts every row of {to_uri!r}"
        )
    # The staged DATASET is the source, streamed by the merge rather than materialised on the driver.
    _converge(to_uri, staged, so, marker, full_sync=True)
    return lance.dataset(to_uri, storage_options=so).count_rows()


def _run_stage(
    from_uri: str,
    to_uri: str,
    stage: str,
    so: StorageOptions,
    *,
    lineage: str = "",
    base_version: int | None = None,
    cardinality: str = ONE_TO_ONE,
    dataset_id: str = "",
    marker: CommitMarker | None = None,
) -> int | None:
    """Run the stage and answer the destination version it ended on, ``None`` when it wrote nothing (an empty delta).

    Every lane's LAST destination commit carries ``marker``, with every commit it cannot mark ordered before it.
    """
    upstream = lance.dataset(from_uri, storage_options=so)
    # THE DELTA BOUNDARY (D1): the order's version floor, so a two-row backfill does not rescan and
    # rewrite the whole tier.
    delta = _delta_filter(base_version)
    if delta is not None and not _mergeable(to_uri, so):
        delta = None  # see `_mergeable`: rebuild whole rather than write a delta into a wiped table
    rows_in = upstream.count_rows()
    rows_out = -1  # set by whichever branch runs; -1 means "read it off the destination"
    # WHICH LANE RAN, said out loud. A delta run and a full rescan produce identical
    # completion lines otherwise, so the one property this change exists to deliver is the
    # one property an operator cannot confirm from the logs. Measured live before adding it:
    # a gold hop with BASE_VERSION=104 was indistinguishable from a full rescan.
    lane = "full"
    # A retraction DELETES from a governed tier, so it is said out loud on every completion line for
    # the same reason the lane is: a destructive step an operator cannot see in the logs is one nobody
    # can attribute a missing row to. Only the delta lane retracts — the others converge with
    # `when_not_matched_by_source_delete`, where the drop is already part of the merge.
    retracted = 0

    if blob_field_names(upstream.schema):
        # MEDIA path: the derivers need the payload bytes, so round-trip + derive via pylance (below).
        _media_transform(from_uri, to_uri, so, stage=stage, lineage=lineage, dataset_id=dataset_id, marker=marker)
    elif delta is not None and base_version is not None:
        # BACKFILL LANE. The delta is by construction small, so it is stamped and merged on the driver
        # — the same argument the cascade head below already makes for handling the bronze root
        # natively rather than distributing it.
        lane = "delta"
        # RETRACTION FIRST, because a deletion-only change IS an empty delta: the version columns
        # describe rows the table still has, so nothing the predicate selects can name a row that is
        # gone. Run after the early return below and a delete would leave through the `delta_empty`
        # door reporting that nothing changed.
        retracted = _retract_deleted(upstream, base_version, to_uri, so)
        source = upstream.to_table(with_row_id=True, filter=delta)
        rows_in = source.num_rows
        if rows_in == 0:
            # A legitimate no-op, not a failure: a redelivered event whose rows this stage already
            # processed lands here. Writing an empty version would fire a publication event for data
            # nobody added. It carries NO marker: a retraction above may have committed, and a reader
            # that finds none resubmits the run, which converges, rather than mistaking it for a write.
            print(f"RAY-STAGE OK stage={stage} lane=delta rows=0 delta_empty=1 retracted={retracted} base_version={base_version}")
            return None
        produced = _stamp_stage(source, stage, lineage, dataset_id, stable_row_ids=upstream.has_stable_row_ids)
        rows_out = produced.num_rows
        # A merge carries ROWS, not schema metadata, so the stamp reaches the dataset only through
        # `ensure_declared_dataset_id` (`service_kit.lakehouse.stage_stamp`). That commit cannot carry the marker, so it
        # lands BEFORE the merge, which can: the merge is then this run's last commit and carries it.
        ensure_declared_dataset_id(to_uri, dataset_id, so)
        _merge_into(to_uri, produced, so, marker)
    elif "source_rowid" not in upstream.schema.names:
        # CASCADE HEAD (tabular): mint root-provenance source_rowid from the upstream _rowid, as a native
        # pylance overwrite on the driver (the bronze root is small); deeper tabular stages, which already
        # CARRY source_rowid as a plain column, distribute below. Same 2.2 + stable-id contract.
        #
        # R27 CORRECTION (2026-07-28): the reason this branch used to give — "lance_ray's distributed read
        # does not surface the reserved _rowid metacolumn" — is FALSE and was never measured.
        # `lr.read_lance(uri, scanner_options={"with_row_id": True})` yields keys ['_rowid', …] (verified at
        # lance-ray 0.4.2 AND 0.5.0), and 0.5.0 additionally exposes `with_metadata=True` for
        # `_rowaddr`/`_fragid`. So the head CAN distribute: read with with_row_id, stamp, and write with
        # `lr.write_lance(..., enable_stable_row_ids=True)` (a 0.5.0 parameter — see the image pins).
        # Left as a driver-side write deliberately: the change is a live-cluster behaviour change to the
        # production cascade head and this audit could not run Ray (worker startup fails in the dev
        # sandbox), so it is recorded as a follow-up to prove on kind, not flipped on a signature read.
        _reset_if_legacy(to_uri, so)
        head_rows = _stamp_stage(upstream.to_table(with_row_id=True), stage, lineage, dataset_id, stable_row_ids=upstream.has_stable_row_ids)
        # A FULL-SYNC MERGE, NOT AN OVERWRITE — the same change the in-process head took 2026-09-06.
        # Overwrite re-mints every `_rowid`, and the tier above resolves its `source_rowid` against
        # exactly those, so a re-derivation silently detached the whole chain (measured live: 8 of 8
        # silver references naming bronze rows that no longer existed). `when_not_matched_by_source_delete`
        # keeps the semantics — the run's output IS the whole tier — while identity survives.
        if _dataset_exists(to_uri, so):
            _converge(to_uri, head_rows, so, marker, full_sync=True)
        else:
            lance.write_dataset(
                head_rows,
                to_uri,
                storage_options=so,
                mode="create",
                data_storage_version="2.2",
                enable_stable_row_ids=True,
                transaction_properties=_properties(marker),
            )
    else:
        # The destination is created with the schema the transform EMITS (see _target_schema): every
        # block lance_ray appends is cast to it positionally, so the two must be one construction.
        out_schema = _target_schema(upstream, stage, lineage, dataset_id)

        import lance_ray as lr  #   # Ray-image only; lazy (see module top)

        # Distributed transform on Ray, then a stable-row-id write: create dst with stable ids (empty, output
        # schema) and distributed-APPEND the Ray fragments into it (the property is dataset-level, so they
        # inherit it). concurrency>1 → fragments written in parallel + one commit. source_rowid is already a
        # plain column in `base`, so it flows through map_batches + write as ordinary data (no distributed
        # _rowid needed) — only the head, handled natively above, has to mint it.
        # Read on the driver, so the closure Ray ships carries a bool rather than a dataset handle.
        stable = bool(upstream.has_stable_row_ids)
        transformed = lr.read_lance(from_uri, storage_options=so).map_batches(
            lambda table: _stamp_stage(table, stage, lineage, dataset_id, stable_row_ids=stable), batch_format="pyarrow"
        )
        # THE DISTRIBUTED OUTPUT LANDS IN A STAGING DATASET, then ONE merge converges it — see
        # `_land_staged` for why an append cannot preserve `_rowid` and why the merge's retraction
        # clause makes an empty staged output catastrophic.
        #
        # The staging path sits UNDER the destination's own prefix, so it inherits whatever credential
        # and bucket policy already reach the tier — a sibling path would need its own grant, and a
        # shared scratch bucket would put one tenant's rows where another's credential reaches. It is
        # keyed by the idempotency key so two concurrent runs of different work cannot land in one
        # staging set; a redelivery of the SAME order reuses its own, which is the convergence the key
        # exists to give.
        staged_uri = f"{to_uri.rstrip('/')}/{_STAGING_DIR}/{os.environ.get('RASK_IDEMPOTENCY_KEY', '') or stage}"
        _reset_if_legacy(to_uri, so)
        lance.write_dataset(
            out_schema.empty_table(),
            staged_uri,
            storage_options=so,
            mode="overwrite",
            data_storage_version="2.2",
            enable_stable_row_ids=True,
        )
        lr.write_lance(
            transformed,
            staged_uri,
            storage_options=so,
            mode="append",
            data_storage_version="2.2",
            concurrency=2,
        )
        if _dataset_exists(to_uri, so):
            try:
                _land_staged(to_uri, staged_uri, so, marker)
            finally:
                _drop_staged(staged_uri, so)
        else:
            # NOTHING TO PRESERVE YET. A destination that does not exist has no `_rowid` to keep, so the
            # staged output becomes the tier directly — and it must be a CREATE with stable ids, since
            # the property cannot be turned on later (`lance_docs/file_format.md`).
            lance.write_dataset(
                lance.dataset(staged_uri, storage_options=so).to_table(),
                to_uri,
                storage_options=so,
                mode="create",
                data_storage_version="2.2",
                enable_stable_row_ids=True,
                transaction_properties=_properties(marker),
            )
            _drop_staged(staged_uri, so)

    out = lance.dataset(to_uri, storage_options=so)
    print(
        f"RAY-STAGE OK stage={stage} lane={lane} rows={out.count_rows()} rows_in={rows_in} retracted={retracted} "
        f"version={out.version} dsv={out.data_storage_version} stable_row_ids={out.has_stable_row_ids} cols={out.schema.names}"
    )
    if not out.has_stable_row_ids:
        raise SystemExit("stage transform lost stable row ids")
    # Provenance is checked with a null-count PUSHDOWN, not by materialising every parent id: this
    # runs against a tier that may hold millions of rows.
    #
    # AN ABSENT COLUMN MEANS EVERY ROW IS PARENTLESS, not none. This read `... else 0`, so a transform
    # that dropped `source_rowid` entirely reported ZERO parentless rows and the contract passed — the
    # check was defeated by committing the violation harder rather than partially. That is the shape a
    # green test proves nothing about, because its subject was never there to inspect.
    written = out.count_rows() if rows_out < 0 else rows_out
    parentless = out.count_rows(filter=f"{SOURCE_ROWID_COLUMN} IS NULL") if SOURCE_ROWID_COLUMN in out.schema.names else written
    _assert_stage_contract(
        rows_in=rows_in,
        rows_out=written,
        cardinality=cardinality,
        parentless=parentless,
    )
    return int(out.version)


if __name__ == "__main__":
    main()
