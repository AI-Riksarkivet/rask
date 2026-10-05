"""Ray stage-transform job for the event-driven medallion cascade: how the Ray engine PRODUCES a stage's rows.

A medallion stage runner submits this through the Ray Jobs REST API in response to its cascade trigger. The job reads
the upstream Lance dataset, stamps the provenance columns through the shared `service_kit.lakehouse.stage_stamp`,
and hands the rows to `service_kit.lakehouse.tier_write`, the stage write the in-process engine
(`medallion.services.compute.transform_stage`) lands its rows through too. Everything from the produced rows to the
run's one marked commit is that module's: the window decision, create-or-converge, the delta lane's retraction,
widening, the declared dataset id, the governance labels, the lineage index and the stage contract. This file holds
only how the Ray engine produces rows, so a write-semantics fix lands once for both engines.

THREE PRODUCERS, chosen by the upstream and the window:

* MEDIA (the upstream carries a blob-v2 column): a pylance scan on the driver, streamed in `MEDIA_BATCH_ROWS` slices,
  for the derivers. Each slice carries its blob columns under the upstream's own field (`tier_write.
  carried_blob_column`: managed payloads as bytes, external rows as pointers), is stamped, and derives an inline
  thumbnail and embedding for an image column. The slices reach the write as one re-runnable stream, which lands
  them one upsert at a time and ends on the run's one marked commit, so the driver holds a slice, never the tier.
* DRIVER TABLE (the cascade head, which mints `source_rowid`, and every delta window): the stamped rows as one table.
* DISTRIBUTED (a deeper tabular hop converging whole): `lance_ray.read_lance(...).map_batches(stamp)` written into a
  staging dataset under the destination's prefix, which the write converges from as a dataset.

Consume-layer provenance (R26): the submitting stage runner hands over this run's `LineageDoc` as
``RASK_LINEAGE_DOCUMENT``, and every producer stamps it as the ``lineage`` column of the rows it writes, so a governed
row is never readable without its provenance. An inherited upstream ``lineage`` cell is dropped first.

Env: RASK_SOURCE_URI RASK_DEST_URI RASK_STAGE [RASK_LINEAGE_DOCUMENT RASK_VERSION_FLOOR RASK_CARDINALITY RASK_DEST_TABLE
     RASK_IDEMPOTENCY_KEY RASK_RUN_ID RASK_OUTCOME_URL]  S3_ENDPOINT S3_KEY S3_SECRET [S3_REGION]
     — the key and run id name the run's commit marker, and the outcome URL is the planner's door it reports its
     terminal to (CP-029). [TRACEPARENT TRACESTATE OTEL_*] — trace continuity across the Ray boundary: when the
     submitting stage runner injected its span and OTLP config, the job runs under one root span parented on that trace.

The job signs with the head pod's static ``S3_KEY``/``S3_SECRET``. A run-bound write vend through the namespace is
LH-129's subject.
"""
# TOKEN-AUTHED CLUSTER (gate 7 / R3): with RAY_AUTH_MODE=token on the head, export
# RAY_AUTH_MODE=token + RAY_AUTH_TOKEN (kubectl get secret rask-ray-auth-token -o
# jsonpath='{.data.auth_token}' | base64 -d) before submitting. `ray job submit` /
# JobSubmissionClient then attach `Authorization: Bearer` themselves; any RAW
# requests/httpx call against the dashboard (:8265) must send that header itself.
# NEVER put the token in runtime_env.env_vars — the jobs API echoes runtime_env back.

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Callable, Iterator
from typing import Any

import lance
import pyarrow as pa

from service_kit.lakehouse import blobs, media, tier_write
from service_kit.lakehouse.commit_marker import CommitMarker
from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base, lance_storage_options
from service_kit.lakehouse.run_outcomes import OutcomeReport, report_outcome
from service_kit.lakehouse.stage_stamp import LINEAGE_COLUMN, ONE_TO_ONE, SOURCE_ROWID_COLUMN, STAGE_COLUMN, stamp_stage


def _storage_options() -> StorageOptions:
    """The estate's builder, not a hand-rolled copy.

    A copy that spelled the credential keys bare (`access_key_id`) made object_store blend the ambient `AWS_*`
    environment with them and sign with a pair belonging to neither identity (measured in-cluster 2026-09-03: a
    `403 SignatureDoesNotMatch` that reads as an expired credential), and one with no `session_token` field cannot hold
    an STS credential at all.
    """
    return lance_storage_options(
        os.environ["S3_ENDPOINT"],
        os.environ["S3_KEY"],
        os.environ["S3_SECRET"],
        os.environ.get("S3_REGION", "us-east-1"),
    )


def _stamp_stage(table: pa.Table, stage: str, lineage: str = "", dataset_id: str = "", *, stable_row_ids: bool) -> pa.Table:
    """The per-stage provenance stamp, the one implementation both engines share (`stage_stamp.stamp_stage`).

    ``stable_row_ids`` is the upstream's `has_stable_row_ids`: the stamp refuses to mint `source_rowid` from an upstream
    without them.
    """
    return stamp_stage(table, stage=stage, stable_row_ids=stable_row_ids, lineage=lineage, dataset_id=dataset_id)


def _target_schema(upstream: lance.LanceDataset, stage: str, lineage: str, dataset_id: str) -> pa.Schema:
    """The schema the distributed producer creates its staging dataset with: the transform's OWN output.

    Derived by running the real stamp over a zero-row slice of the real upstream, never rebuilt from a field list:
    `lance_ray` casts each appended block to the staging dataset's schema BY POSITION
    (`lance_ray/pandas.py::pd_to_arrow` -> `df.cast(schema)`), and the stamp re-stamps `stage` and `lineage` IN PLACE,
    so a schema assembled by dropping and re-appending them transposes a pair and the append dies with
    `LanceError(Arrow): … field names are not matching`. One function answers for both sides.
    """
    return _stamp_stage(upstream.schema.empty_table(), stage, lineage, dataset_id, stable_row_ids=upstream.has_stable_row_ids).schema


#: How many rows one media slice carries. The producer holds ~4x one slice of payloads (the scan, the bytes, the
#: thumbnails, the embeddings), and `tier_write` lands each slice as its own upsert, so a slice, not the tier, is what
#: the write holds. At ~1.8 MB page images 128 rows is a few hundred MB. `RASK_STAGE_MEDIA_BATCH_ROWS` tunes it for
#: other payload sizes (ray-project's `patterns/generators.rst`: yield in chunks, never materialise).
MEDIA_BATCH_ROWS = int(os.environ.get("RASK_STAGE_MEDIA_BATCH_ROWS", "128"))

#: How many bytes the media scan may buffer from storage ahead of the slice being produced. Lance's default is 2 GB,
#: and a scan may hold up to twice it (`lance_docs/guide.md:3050-3071`): measured on pylance 12.0.0 over blob-v2 rows
#: of 1 MiB, a default-buffered scan's VmHWM grew from 0.55 to 0.87 GB between 0.2 and 0.8 GB of payload, and from 0.29
#: to 0.34 GB with a 16 MiB buffer and one batch and one fragment of readahead. `RASK_STAGE_MEDIA_IO_BUFFER_BYTES`
#: tunes it.
MEDIA_IO_BUFFER_BYTES = int(os.environ.get("RASK_STAGE_MEDIA_IO_BUFFER_BYTES", str(64 << 20)))


def _media_scanner(ds: lance.LanceDataset, **kwargs: Any) -> lance.LanceScanner:
    """A scan of the media upstream whose read-ahead is bounded by the slice rather than by Lance's 2 GB default."""
    return ds.scanner(batch_size=MEDIA_BATCH_ROWS, batch_readahead=1, fragment_readahead=1, io_buffer_size=MEDIA_IO_BUFFER_BYTES, **kwargs)


def _derivable_blob_column(ds: lance.LanceDataset, blob_cols: list[str]) -> str | None:
    """Which blob column (if any) gets a thumbnail and an embedding, decided ONCE before the stream.

    The derived columns are part of the SCHEMA, so every slice must agree; the probe scans forward for the first
    non-null payload, the same "first non-null decides" contract as `derivers.derive_artifacts`. A tier that already
    carries the artifacts derives nothing, so a second hop carries them forward instead of appending a duplicate
    `thumbnail` (`LanceError(Schema): Duplicate field name`).
    """
    if any(name in ds.schema.names for name in media.ARTIFACT_COLUMNS):
        return None
    for name in blob_cols:
        scanner = _media_scanner(ds, columns=[name], blob_handling="all_binary")
        for batch in scanner.to_batches():
            probe = next((p for p in batch.column(name).to_pylist() if p is not None), None)
            if probe is None:
                continue
            return name if media.is_image(probe) else None
    return None


def _media_rows(ds: lance.LanceDataset, *, stage: str, lineage: str, dataset_id: str) -> Callable[[], pa.RecordBatchReader]:
    """The MEDIA producer: a factory of one stream of stamped, derived slices, each `MEDIA_BATCH_ROWS` rows.

    A factory, because `tier_write` re-plans a marked merge that lost a commit race and a consumed stream cannot be
    read twice. Null-safe by construction (R27): one scan carries tabular and blob columns row-aligned with nulls
    intact, so a null payload carries forward as a null blob with null artifacts. ``with_row_id`` lets the head mint
    `source_rowid` from the same scan, and lets an external upstream's managed rows be read by row id.
    """
    blob_cols = blobs.blob_field_names(ds.schema)
    external_base = blobs.external_base_of(ds)
    handling = tier_write.blob_handling(external_base)
    carried = [f.name for f in ds.schema if f.name not in (STAGE_COLUMN, LINEAGE_COLUMN)]
    derive_from = _derivable_blob_column(ds, blob_cols)

    def produce(aligned: pa.Table) -> pa.Table:
        return _media_batch(ds, aligned, blob_cols, derive_from, external_base, stage=stage, lineage=lineage, dataset_id=dataset_id)

    # The schema of an empty slice, so a source of zero rows still creates the tier with every column it would carry.
    # Bounded like every media scan: under Lance's default buffer this `limit=0` scan still read ahead through the
    # payloads (measured on pylance 12.0.0: a 1.87 GB peak over 0.8 GB of 1 MiB blobs, before any slice was produced).
    schema = produce(_media_scanner(ds, columns=carried, blob_handling=handling, with_row_id=True, limit=0).to_table()).schema

    def stream() -> pa.RecordBatchReader:
        scanner = _media_scanner(ds, columns=carried, blob_handling=handling, with_row_id=True)
        slices = (out for raw in scanner.to_batches() for out in produce(pa.Table.from_batches([raw])).to_batches())
        return pa.RecordBatchReader.from_batches(schema, slices)

    return stream


def _media_batch(
    ds: lance.LanceDataset,
    aligned: pa.Table,
    blob_cols: list[str],
    derive_from: str | None,
    external_base: str | None,
    *,
    stage: str,
    lineage: str,
    dataset_id: str = "",
) -> pa.Table:
    """One slice: carry its blobs under the upstream's fields, stamp its provenance, derive its artifacts.

    Every slice takes the same branches and therefore produces the same schema, which is what lets the write take
    them as one stream. `_rowid` reaches the stamp, which mints `source_rowid` from it at the cascade head and drops it.
    """
    row_ids = aligned.column("_rowid").to_pylist()
    columns: dict[str, Any] = {}
    fields: list[pa.Field] = []
    for name in aligned.schema.names:
        if name in blob_cols:
            field, columns[name] = tier_write.carried_blob_column(ds, name, aligned.column(name).to_pylist(), row_ids, external_base)
            fields.append(field)
        else:
            fields.append(aligned.schema.field(name))
            columns[name] = aligned.column(name)
    out = _stamp_stage(pa.table(columns, schema=pa.schema(fields)), stage, lineage, dataset_id, stable_row_ids=ds.has_stable_row_ids)

    # Row-wise, image payloads only: a payload past the header probe that fails full decode raises and FAILs the run;
    # a NULL payload (absent bytes, not bad bytes) keeps its row with null artifacts. An external upstream's scan holds
    # descriptors, so its bytes are read by row id (`read_blobs` keeps a null row's slot, measured on pylance 12.0.0).
    if derive_from is not None:
        if external_base and row_ids:
            payloads = [payload for _, payload in ds.read_blobs(derive_from, ids=row_ids, preserve_order=True)]
        elif external_base:
            payloads = []
        else:
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


#: Where the distributed producer stages its output before the write converges it.
#:
#: UNDER THE DESTINATION'S OWN PREFIX on purpose: the staging set inherits the credential and bucket policy that
#: already reach the tier, where a sibling path would need its own grant and a shared scratch bucket would put one
#: tenant's rows where another's credential reaches. NAMED IN the maintenance walk's `_CONTROL_PREFIXES`, because a
#: staging set is a real Lance dataset while it exists and, on the run that creates the destination, its parent is
#: still a plain directory the walk would descend.
_STAGING_DIR = "_staging"


class StagedOutputMissingError(RuntimeError):
    """The distributed write left nothing at the staging path, which is not the same as producing no rows."""


def _drop_staged(staged_uri: str, so: StorageOptions) -> None:
    """Remove a staging set, best-effort: the write has already landed when this runs.

    NEVER RAISES, because a propagating error would turn a landed stage into a reported failure and invite a re-run of
    finished work. NEVER SILENT EITHER: the `_staging` control prefix keeps the maintenance walk off the set, so an
    abandoned one is invisible unless this line names its path.
    """
    try:
        fs, base = fs_and_base(staged_uri, so)
        fs.delete_dir(base)
    # Broad on purpose: the landing already succeeded, so no failure shape here may undo it.
    except Exception as exc:
        print(f"RAY-STAGE WARN staging set left behind at {staged_uri}: {type(exc).__name__}: {exc}")


def _distributed_rows(
    upstream: lance.LanceDataset, from_uri: str, staged_uri: str, so: StorageOptions, *, stage: str, lineage: str, dataset_id: str
) -> lance.LanceDataset:
    """The DISTRIBUTED producer: the stamped rows written by Ray workers into a staging dataset, answered as that dataset.

    `lance_ray.write_lance` offers create, append and overwrite but no distributed merge, and `enable_stable_row_ids`
    is create-time-only, so the fragments land in a staging set first and the write converges from it, keeping every
    surviving row's `_rowid` (an append would assign new ones, `lance_docs/file_format.md:3998`). `source_rowid` is
    already a plain column on a deeper hop, so it flows through `map_batches` as ordinary data.

    Raises:
        StagedOutputMissingError: the distributed write committed nothing at the staging path.
    """
    import lance_ray as lr  # Ray-image only, and imported here so the producers above import without Ray.

    # Read on the driver, so the closure Ray ships carries a bool rather than a dataset handle.
    stable = bool(upstream.has_stable_row_ids)
    transformed = lr.read_lance(from_uri, storage_options=so).map_batches(
        lambda table: _stamp_stage(table, stage, lineage, dataset_id, stable_row_ids=stable), batch_format="pyarrow"
    )
    lance.write_dataset(
        _target_schema(upstream, stage, lineage, dataset_id).empty_table(),
        staged_uri,
        storage_options=so,
        mode="overwrite",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    lr.write_lance(transformed, staged_uri, storage_options=so, mode="append", data_storage_version="2.2", concurrency=2)
    try:
        return lance.dataset(staged_uri, storage_options=so)
    except ValueError as exc:
        raise StagedOutputMissingError(f"the distributed write left no dataset at {staged_uri!r}") from exc


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
    # The write's own log lines (a re-planned merge, a disarmed tier, the landing) go to the job's log.
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(name)s %(message)s")
    so = _storage_options()
    # THE PLATFORM'S OWN VOCABULARY — `WorkOrder.to_env()`, the ONE serialization, so no adapter hand-rolls it. A job
    # reading a name no order supplies binds it to the empty string, and an empty source URI is a run that scans
    # nothing and reports success. Pinned by `tests/unit/test_the_submitter_and_the_job_agree_on_the_wire.py`.
    from_uri, to_uri, stage = os.environ["RASK_SOURCE_URI"], os.environ["RASK_DEST_URI"], os.environ["RASK_STAGE"]
    lineage = os.environ.get("RASK_LINEAGE_DOCUMENT", "")
    # THE DELTA BOUNDARY. An order OMITS the floor when there is none rather than blanking it; both spellings arrive
    # here as absent, which is the same answer.
    raw_floor = os.environ.get("RASK_VERSION_FLOOR", "").strip()
    version_floor = int(raw_floor) if raw_floor else None
    cardinality = os.environ.get("RASK_CARDINALITY", "").strip() or ONE_TO_ONE
    # THE DESTINATION'S canonical catalog name, stamped onto the schema this run writes. Absent means unwired, and the
    # stamp then DROPS the inherited one rather than publishing a name that describes another dataset.
    dataset_id = os.environ.get("RASK_DEST_TABLE", "").strip()
    # THE RUN'S ONE NAME, and where it reports (CP-029): the order's key is the plan's action id, stamped on the run's
    # last commit; an order with no key marks nothing, one with no door reports nothing and the plan's sweep resolves it.
    action_id = os.environ.get("RASK_IDEMPOTENCY_KEY", "").strip()
    marker = CommitMarker(action_id=action_id, run_id=os.environ.get("RASK_RUN_ID", "").strip()) if action_id else None
    outcome_url = os.environ.get("RASK_OUTCOME_URL", "").strip()
    try:
        with _traced_root("ray.stage_job", {"lance.medallion.stage": stage}):
            version = _run_stage(
                from_uri, to_uri, stage, so, lineage=lineage, base_version=version_floor, cardinality=cardinality, dataset_id=dataset_id, marker=marker
            )
    except BaseException as exc:
        # A run that committed and THEN failed (a contract check after the write) still reports failed: the planner
        # reads the destination's history for the marker, so the version it committed is recorded on the FAIL.
        if outcome_url:
            report_outcome(outcome_url, OutcomeReport(status="failed", error=f"{type(exc).__name__}: {exc}"[:4000]))
        raise
    if outcome_url:
        report_outcome(outcome_url, OutcomeReport(status="succeeded", committed_version=version))


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
    """Produce the stage's rows and land them through `tier_write`; answer the marked version, ``None`` for an empty delta."""
    upstream = lance.dataset(from_uri, storage_options=so)
    target = tier_write.TierTarget(uri=to_uri, storage_options=so, dataset_id=dataset_id, cardinality=cardinality, marker=marker)
    window = tier_write.plan_window(upstream, target, base_version)
    staged_uri: str | None = None
    rows: tier_write.RowSource
    if blobs.blob_field_names(upstream.schema):
        producer = "media"
        rows = _media_rows(upstream, stage=stage, lineage=lineage, dataset_id=dataset_id)
    elif window.floor is not None or SOURCE_ROWID_COLUMN not in upstream.schema.names:
        # The cascade head mints root provenance from the upstream `_rowid`, and a delta is small by construction:
        # both are stamped on the driver.
        producer = "driver"
        source = upstream.to_table(with_row_id=True, filter=window.row_filter)
        rows = _stamp_stage(source, stage, lineage, dataset_id, stable_row_ids=upstream.has_stable_row_ids)
    else:
        producer = "distributed"
        # Keyed by the run's key so two concurrent runs of different work never share a staging set, while a
        # redelivery of the same order reuses its own.
        staged_uri = f"{to_uri.rstrip('/')}/{_STAGING_DIR}/{marker.action_id if marker is not None else stage}"
        rows = _distributed_rows(upstream, from_uri, staged_uri, so, stage=stage, lineage=lineage, dataset_id=dataset_id)
    try:
        written = tier_write.write_tier(upstream, rows, window, target)
    finally:
        if staged_uri is not None:
            _drop_staged(staged_uri, so)
    print(
        f"RAY-STAGE OK stage={stage} producer={producer} lane={written.lane} rows_in={written.rows_in} rows_out={written.rows_out} "
        f"retracted={written.retracted} version={written.version} base_version={base_version}"
    )
    return written.version


if __name__ == "__main__":
    main()
