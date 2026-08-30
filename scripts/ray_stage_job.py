"""Ray Data stage-transform job for the EVENT-DRIVEN medallion cascade.

A medallion mover submits this via the Ray Jobs REST API (services/medallion/services/ray_submit.py) IN
RESPONSE TO its Dapr cascade trigger — the production-shape replacement for the in-process fake-Ray
``compute.transform_stage``. It reads the upstream Lance dataset, stamps a ``stage`` provenance column across
Ray workers, threads the row-level ``source_rowid`` provenance column (minted at the head from ``_rowid``,
carried forward — parity with the in-process path), and writes the downstream dataset at file format 2.2 with
stable row ids (create the target with
stable ids, then distributed-append — lance_ray.write_lance has no stable-row-ids param). The mover then reads
the written version + statistics for the OpenLineage WROTE edge, exactly as the in-process path does.

TWO paths, chosen by whether the upstream carries a blob-v2 column:
* TABULAR → the distributed lance_ray read→map_batches(stamp)→write path (Ray workers, one commit).
* MEDIA (blob-v2 present) → a pylance-native round-trip on the driver: lance_ray's write strips blob
  typing (exposes plain LargeBinary), so a blob column must be re-materialised via a
  ``blob_handling="all_binary"`` scan (NOT ``read_blobs`` — it drops null rows) and
  re-wrapped with ``blob_array`` before a 2.2 write, and image payloads get an inline thumbnail +
  embedding derived here. This is the SAME contract as compute.transform_stage / derivers, and by the
  same code: the derivers are IMPORTED from ``service_kit.lakehouse.media``, not inlined and
  drift-pinned (B14 — the pin was the wrong fix, and this docstring described it long after the copies
  were gone). Closes the Phase-3 gap that forced media stages onto the in-process fallback.

Consume-layer provenance (R26): the submitting mover hands over this run's ``LineageDoc`` as
``LINEAGE_JSON``, and every path below writes it as the ``lineage`` column (Arrow JSON → Lance JSONB) in
the SAME commit as the data — a governed row must never be readable without its provenance, and the
distributed path must not produce a dataset the in-process path would have stamped. Any upstream
``lineage`` cell is DROPPED first: it describes the parent's run, not this one. Unset/empty → no column
(the pre-R26 shape), so the job stays runnable by hand.

Env: FROM_URI TO_URI STAGE [LINEAGE_JSON]  S3_ENDPOINT S3_KEY S3_SECRET [S3_REGION]
     [TRACEPARENT TRACESTATE OTEL_*] — trace continuity across the Ray boundary (prod-readiness P3):
     when the submitting mover injected its span + OTLP config, the job runs under one root span
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
import os
import sys
from collections.abc import Iterator
from typing import Any

import lance
import pyarrow as pa
import pyarrow.fs as pafs
from lance import blob_array, blob_field

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
from service_kit.lakehouse.stage_stamp import CARDINALITIES, LINEAGE_COLUMN, ONE_TO_ONE, SOURCE_ROWID_COLUMN, STAGE_COLUMN


def _storage_options() -> dict[str, str]:
    return {
        "endpoint": os.environ["S3_ENDPOINT"],
        "access_key_id": os.environ["S3_KEY"],
        "secret_access_key": os.environ["S3_SECRET"],
        "region": os.environ.get("S3_REGION", "us-east-1"),
        "allow_http": "true",
        "virtual_hosted_style_request": "false",
    }


def _reset_dataset(to_uri: str, so: dict[str, str]) -> None:
    """Delete any existing dataset at ``to_uri`` so the create-with-stable-ids below is truly fresh.

    ``enable_stable_row_ids`` is a create-time-only property: ``mode="overwrite"`` on a dataset that already
    exists WITHOUT stable ids (e.g. one a prior in-process run created) does NOT flip it on. The cascade uses
    overwrite semantics (each run's output IS the whole dataset), so clearing the dir first is correct here.
    """
    endpoint = so["endpoint"]
    scheme, _, host = endpoint.partition("://")
    fs = pafs.S3FileSystem(
        endpoint_override=host or endpoint,
        access_key=so["access_key_id"],
        secret_key=so["secret_access_key"],
        region=so.get("region", "us-east-1"),
        scheme=scheme if host else "http",
    )
    with contextlib.suppress(OSError):
        fs.delete_dir_contents(to_uri.removeprefix("s3://"), missing_dir_ok=True)


def _reset_if_legacy(to_uri: str, so: dict[str, str]) -> None:
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


def _stamp_stage(table: pa.Table, stage: str, lineage: str = "") -> pa.Table:
    """The per-stage provenance stamp — delegated to the ONE implementation both drivers share.

    This was a hand-maintained mirror of the medallion's copy and it had already drifted: on a
    re-stamp it dropped `stage` and re-appended it at the end while the in-process driver replaced it
    in place, so a silver table's column ORDER — and therefore its dataset schema, since
    `write_dataset(mode="overwrite")` takes the table's — depended on which compute path wrote it.

    It is now the ONLY thing in this job that decides where a provenance column sits: the media lane
    stamps through it, the distributed lane's blocks come out of it, and the schema the distributed
    lane creates its destination with is derived from it (:func:`_target_schema`).
    """
    from service_kit.lakehouse.stage_stamp import stamp_stage

    return stamp_stage(table, stage=stage, lineage=lineage)


def _target_schema(upstream: lance.LanceDataset, stage: str, lineage: str) -> pa.Schema:
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
    return _stamp_stage(upstream.schema.empty_table(), stage, lineage).schema


#: How many rows the media lane holds in the driver at once.
#:
#: The MEDIA branch cannot go through lance_ray (its write strips blob typing), so the round-trip
#: happens on the driver — but it used to happen ALL AT ONCE: one `to_table()` over every blob
#: payload, a second full copy as Python bytes from `to_pylist()`, and two more as the thumbnail and
#: embedding lists. Peak RSS scaled with the dataset, so the cascade had an OOM ceiling nothing
#: announced (ray-project's own `patterns/generators.rst`: yield in chunks rather than materialise).
#: Bounded, the driver holds ~4x one batch of payloads; at the media lane's ~1.8 MB page images 128
#: rows is a few hundred MB. `RASK_STAGE_MEDIA_BATCH_ROWS` tunes it for other payload sizes.
MEDIA_BATCH_ROWS = int(os.environ.get("RASK_STAGE_MEDIA_BATCH_ROWS", "128"))


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


def _media_transform(from_uri: str, to_uri: str, so: dict[str, str], *, stage: str, lineage: str = "") -> None:
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
    batch, so what the driver holds is bounded by the batch rather than by the run. The FIRST write
    overwrites and the rest append: that keeps the create-time-only ``enable_stable_row_ids``
    contract exactly as before, and it is what stops a rerun of the same stage from doubling the
    table instead of replacing it.
    """
    ds = lance.dataset(from_uri, storage_options=so)
    blob_cols = blob_field_names(ds.schema)
    carried = [f.name for f in ds.schema if f.name not in (STAGE_COLUMN, LINEAGE_COLUMN)]
    derive_from = _derivable_blob_column(ds, blob_cols)

    # with_row_id so the head can mint source_rowid from the SAME aligned scan (a carried source_rowid is a
    # plain column already in this read); mirrors compute._carry_forward's blob path.
    scanner = ds.scanner(columns=carried, blob_handling="all_binary", with_row_id=True, batch_size=MEDIA_BATCH_ROWS)

    written = 0
    for batch in scanner.to_batches():
        out = _media_batch(pa.Table.from_batches([batch]), blob_cols, derive_from, stage=stage, lineage=lineage)
        # Same overwrite contract as the in-process compute.transform_stage: enable_stable_row_ids is
        # create-time-only, so a first write creates the target with stable ids and later runs overwrite in
        # place keeping them. A legacy no-stable-id target is migrated once (the tabular path's reset); the
        # media lane's silver dataset is always created BY this contract, so no reset is needed here.
        lance.write_dataset(
            out,
            to_uri,
            mode="overwrite" if written == 0 else "append",
            storage_options=so,
            data_storage_version="2.2",
            enable_stable_row_ids=True,
        )
        written += out.num_rows

    if written == 0:
        # An empty source still has to produce the target — an absent dataset is not the same answer
        # as an empty one, and the tier's readers open it either way.
        empty = _media_batch(
            ds.scanner(columns=carried, blob_handling="all_binary", with_row_id=True, limit=0).to_table(), blob_cols, derive_from, stage=stage, lineage=lineage
        )
        lance.write_dataset(empty, to_uri, mode="overwrite", storage_options=so, data_storage_version="2.2", enable_stable_row_ids=True)


def _media_batch(aligned: pa.Table, blob_cols: list[str], derive_from: str | None, *, stage: str, lineage: str) -> pa.Table:
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
    out = _stamp_stage(pa.table(columns, schema=pa.schema(fields)), stage, lineage)

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
    from_uri, to_uri, stage = os.environ["FROM_URI"], os.environ["TO_URI"], os.environ["STAGE"]
    lineage = os.environ.get("LINEAGE_JSON", "")  # this run's consume-layer provenance document (R26)
    # THE DELTA BOUNDARY, finally read. `submit_stage_job` has exported it since the publication event
    # started carrying {from_version, to_version}; this job ignored it, so every run rescanned the tier.
    # Blank means a full run — a first stage has no boundary to be incremental against.
    raw_base = os.environ.get("BASE_VERSION", "").strip()
    base_version = int(raw_base) if raw_base else None
    # The lane's declared row cardinality. Absent means 1:1, which is what every default mover is and
    # what the old unconditional assertion enforced — so an un-migrated lane behaves exactly as before.
    cardinality = os.environ.get("STAGE_CARDINALITY", "").strip() or ONE_TO_ONE

    # Continue the submitting mover's trace (P3): the whole stage transform runs as one child span of
    # the mover's medallion.transform span; without a handed-over context it runs exactly as before.
    with _traced_root("ray.stage_job", {"lance.medallion.stage": stage}):
        _run_stage(from_uri, to_uri, stage, so, lineage=lineage, base_version=base_version, cardinality=cardinality)


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

    ``_row_created_at_version`` requires ``enable_stable_row_ids`` AT CREATION — setting it later is a
    silent no-op — which is why the catalog's creation contract enforces it and why every write in
    this file passes it. `None` means "everything": a first run has no boundary to be incremental
    against, and filtering against version 0 would be the same answer at more cost.
    """
    return None if base_version is None else f"_row_created_at_version > {base_version}"


def _mergeable(to_uri: str, so: dict[str, str]) -> bool:
    """Can this destination take a delta, or must the run rebuild it whole?

    A destination that does not exist yet, or one written before stable row ids, cannot accept a
    merge — and `_reset_if_legacy` would WIPE the legacy one. Writing only a delta into a table that
    was just wiped is silent data loss, so a delta run over such a destination degrades to a full run
    instead. It becomes mergeable on the next run, because the full run creates it with stable ids.
    """
    with contextlib.suppress(Exception):
        return bool(lance.dataset(to_uri, storage_options=so).has_stable_row_ids)
    return False


def _merge_into(to_uri: str, table: pa.Table, so: dict[str, str]) -> None:
    """Converge this run's rows into the destination on the tier's key.

    `merge_insert`, never `append`: Dapr delivers at least once, so a redelivered publication event
    WILL re-run this stage over the same delta, and an append would double every row of it with
    nothing downstream noticing. `id` is a tier-contract column (`TIER_COLUMNS`), not a workload
    assumption, so merging on it is the platform's to do.
    """
    lance.dataset(to_uri, storage_options=so).merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(table)


def _run_stage(
    from_uri: str,
    to_uri: str,
    stage: str,
    so: dict[str, str],
    *,
    lineage: str = "",
    base_version: int | None = None,
    cardinality: str = ONE_TO_ONE,
) -> None:
    upstream = lance.dataset(from_uri, storage_options=so)
    # THE DELTA BOUNDARY (D1). `submit_stage_job` has always exported BASE_VERSION and this job never
    # read it, so every run — a two-row backfill included — rescanned and rewrote the whole tier.
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

    if blob_field_names(upstream.schema):
        # MEDIA path: lance_ray strips blob typing on write, so round-trip + derive via pylance (below).
        _media_transform(from_uri, to_uri, so, stage=stage, lineage=lineage)
    elif delta is not None:
        # BACKFILL LANE. The delta is by construction small, so it is stamped and merged on the driver
        # — the same argument the cascade head below already makes for handling the bronze root
        # natively rather than distributing it.
        lane = "delta"
        source = upstream.to_table(with_row_id=True, filter=delta)
        rows_in = source.num_rows
        if rows_in == 0:
            # A legitimate no-op, not a failure: a redelivered event whose rows this stage already
            # processed lands here. Writing an empty version would fire a publication event for data
            # nobody added.
            print(f"RAY-STAGE OK stage={stage} lane=delta rows=0 delta_empty=1 base_version={base_version}")
            return
        produced = _stamp_stage(source, stage, lineage)
        rows_out = produced.num_rows
        _merge_into(to_uri, produced, so)
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
        lance.write_dataset(
            _stamp_stage(upstream.to_table(with_row_id=True), stage, lineage),
            to_uri,
            storage_options=so,
            mode="overwrite",
            data_storage_version="2.2",
            enable_stable_row_ids=True,
        )
    else:
        # The destination is created with the schema the transform EMITS (see _target_schema): every
        # block lance_ray appends is cast to it positionally, so the two must be one construction.
        out_schema = _target_schema(upstream, stage, lineage)

        import lance_ray as lr  #   # Ray-image only; lazy (see module top)

        # Distributed transform on Ray, then a stable-row-id write: create dst with stable ids (empty, output
        # schema) and distributed-APPEND the Ray fragments into it (the property is dataset-level, so they
        # inherit it). concurrency>1 → fragments written in parallel + one commit. source_rowid is already a
        # plain column in `base`, so it flows through map_batches + write as ordinary data (no distributed
        # _rowid needed) — only the head, handled natively above, has to mint it.
        transformed = lr.read_lance(from_uri, storage_options=so).map_batches(lambda table: _stamp_stage(table, stage, lineage), batch_format="pyarrow")
        _reset_if_legacy(to_uri, so)
        lance.write_dataset(
            out_schema.empty_table(),
            to_uri,
            storage_options=so,
            mode="overwrite",
            data_storage_version="2.2",
            enable_stable_row_ids=True,
        )
        lr.write_lance(
            transformed,
            to_uri,
            storage_options=so,
            mode="append",
            data_storage_version="2.2",
            concurrency=2,
        )

    out = lance.dataset(to_uri, storage_options=so)
    print(
        f"RAY-STAGE OK stage={stage} lane={lane} rows={out.count_rows()} rows_in={rows_in} version={out.version} "
        f"dsv={out.data_storage_version} stable_row_ids={out.has_stable_row_ids} cols={out.schema.names}"
    )
    if not out.has_stable_row_ids:
        raise SystemExit("stage transform lost stable row ids")
    # Provenance is checked with a null-count PUSHDOWN, not by materialising every parent id: this
    # runs against a tier that may hold millions of rows.
    parentless = out.count_rows(filter=f"{SOURCE_ROWID_COLUMN} IS NULL") if SOURCE_ROWID_COLUMN in out.schema.names else 0
    _assert_stage_contract(
        rows_in=rows_in,
        rows_out=out.count_rows() if rows_out < 0 else rows_out,
        cardinality=cardinality,
        parentless=parentless,
    )


if __name__ == "__main__":
    main()
