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
  typing (exposes plain LargeBinary), so a blob column must be re-materialised via ``read_blobs`` and
  re-wrapped with ``blob_array`` before a 2.2 write, and image payloads get an inline thumbnail +
  embedding derived here. This is the SAME contract as compute.transform_stage / derivers — the deriver
  is inlined (self-contained job, like ray_train_job) and drift-pinned to services/medallion by a unit
  test. Closes the Phase-3 gap that forced media stages onto the in-process fallback (Ray blob parity).

Env: FROM_URI TO_URI STAGE  S3_ENDPOINT S3_KEY S3_SECRET [S3_REGION]
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
import io
import os
import sys
import warnings
from collections.abc import Iterator
from typing import Any

import lance
import pyarrow as pa
import pyarrow.fs as pafs
from lance import blob_array, blob_field


# lance_ray ships in the Ray image, NOT our services' venv — imported LAZILY (inside the tabular branch
# of main) so the deriver primitives below stay importable in the unit venv for the drift-pin test
# (tests/unit/test_ray_stage_job.py), exactly as ray_train_job keeps `lance` out of its module top.

# --- blob + deriver primitives, inlined + drift-pinned to services (test_ray_stage_job.py) --------------
# Kept byte-identical to service_kit.lakehouse.blobs.is_blob_field / medallion.services.media so the Ray path and the
# in-process path derive the SAME thumbnail/embedding; the pin test fails if either side drifts.
_BLOB_V2_EXTENSION_NAME = "lance.blob.v2"  # == service_kit.lakehouse.blobs.BLOB_V2_EXTENSION_NAME
_THUMBNAIL_SIZE = (128, 128)  # == media.THUMBNAIL_SIZE
_EMBEDDING_DIMS = 8  # == media.EMBEDDING_DIMS


def _is_blob_field(field: pa.Field) -> bool:
    """Mirror of service_kit.lakehouse.blobs.is_blob_field: registered extension name, else the raw field metadata."""
    if getattr(field.type, "extension_name", None) == _BLOB_V2_EXTENSION_NAME:
        return True
    metadata = field.metadata or {}
    return metadata.get(b"ARROW:extension:name") == _BLOB_V2_EXTENSION_NAME.encode()


def _blob_field_names(schema: pa.Schema) -> list[str]:
    return [f.name for f in schema if _is_blob_field(f)]


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


def _stamp_stage(table: pa.Table, stage: str) -> pa.Table:
    """Carry every upstream column forward, thread root-provenance ``source_rowid``, and (re)stamp ``stage``
    — the generic per-stage transform, type-preserving via the pyarrow batch format.

    Mirrors compute._carry_source_rowid + _stamp_stage: an upstream that already carries ``source_rowid``
    keeps it (a later stage), the cascade head mints it from the reserved ``_rowid`` metacolumn (durable
    under stable row ids), and ``_rowid`` itself is never persisted (reserved name; advances on overwrite).
    """
    if "source_rowid" not in table.column_names and "_rowid" in table.column_names:
        table = table.append_column(pa.field("source_rowid", pa.uint64()), table.column("_rowid").cast(pa.uint64()))
    if "_rowid" in table.column_names:
        table = table.drop_columns(["_rowid"])
    if "stage" in table.column_names:
        table = table.drop_columns(["stage"])
    return table.append_column("stage", pa.array([stage] * table.num_rows, pa.string()))


def _open_guarded(payload: bytes):  # -> PIL.Image.Image
    """Mirror of media._open_guarded: decompression-bomb-guarded open (Pillow default only trips at 2×)."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 64_000_000
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        return Image.open(io.BytesIO(payload))


def _derive_thumbnail(image_bytes: bytes) -> bytes:
    """Mirror of media.derive_thumbnail: a downscaled inline PNG thumbnail."""
    from PIL import Image  # noqa: F401 — ensures the guarded open above ran Image config

    with _open_guarded(image_bytes) as image:
        thumb = image.convert("RGB")
        thumb.thumbnail(_THUMBNAIL_SIZE)
        buffer = io.BytesIO()
        thumb.save(buffer, format="PNG")
        return buffer.getvalue()


def _derive_embedding(image_bytes: bytes) -> list[float]:
    """Mirror of media.derive_embedding: deterministic luminance downsample to _EMBEDDING_DIMS floats."""
    from PIL import Image

    with _open_guarded(image_bytes) as image:
        columns = image.convert("L").resize((_EMBEDDING_DIMS, 1), resample=Image.Resampling.BILINEAR)
        return [value / 255.0 for value in columns.tobytes()]


def _is_image(payload: bytes) -> bool:
    """Mirror of media.is_image: decode-probe the payload (verify headers, any failure → not an image)."""
    try:
        with _open_guarded(payload) as image:
            image.verify()
    except Exception:
        return False
    return True


def _media_transform(from_uri: str, to_uri: str, so: dict[str, str], *, stage: str) -> None:
    """The MEDIA path: pylance-native blob round-trip + inline image derivation, then a 2.2 stable-id write.

    Same contract as compute.transform_stage + derivers.derive_artifacts: re-materialise each blob column
    via ``read_blobs`` and re-wrap with ``blob_array`` (lance_ray's write would demote it to plain binary),
    stamp ``stage``, and for a blob column whose FIRST payload decodes as an image, append an inline
    ``thumbnail`` (PNG) + ``embedding`` (fixed-size floats). Non-image blobs carry through untouched.
    """
    ds = lance.dataset(from_uri, storage_options=so)
    blob_cols = _blob_field_names(ds.schema)
    rows = ds.count_rows()
    # with_row_id so the head can mint source_rowid from the SAME aligned scan (a carried source_rowid is a
    # plain column already in this read); mirrors compute._carry_forward's blob path.
    plain = ds.to_table(
        columns=[f.name for f in ds.schema if f.name not in blob_cols and f.name != "stage"],
        with_row_id=True,
    )

    columns: dict = {}
    fields: list[pa.Field] = []
    first_payloads: dict[str, list[bytes]] = {}
    for f in ds.schema:
        if f.name == "stage":
            continue  # re-stamped below
        if f.name in blob_cols:
            payloads = [payload for _addr, payload in ds.read_blobs(f.name, indices=list(range(rows)))]
            first_payloads[f.name] = payloads
            fields.append(blob_field(f.name))
            columns[f.name] = blob_array(payloads)
        else:
            fields.append(plain.schema.field(f.name))
            columns[f.name] = plain.column(f.name)
    # Root provenance (mirror compute._carry_source_rowid): carry source_rowid if the upstream has it, else
    # mint it from the aligned _rowid at the head. _rowid is never persisted.
    if "source_rowid" not in columns:
        fields.append(pa.field("source_rowid", pa.uint64()))
        columns["source_rowid"] = plain.column("_rowid").cast(pa.uint64())
    fields.append(pa.field("stage", pa.string()))
    columns["stage"] = pa.array([stage] * rows, pa.string())
    out = pa.table(columns, schema=pa.schema(fields))

    # Derive from the first IMAGE blob column (the media lane has exactly one: `payload`). Row-wise, image
    # payloads only — a payload past the header probe that fails full decode raises, FAILing the run.
    for payloads in first_payloads.values():
        if payloads and _is_image(payloads[0]):
            thumbnails = [_derive_thumbnail(p) for p in payloads]
            embeddings = [_derive_embedding(p) for p in payloads]
            out = out.append_column(pa.field("thumbnail", pa.large_binary()), pa.array(thumbnails, pa.large_binary()))
            out = out.append_column(
                pa.field("embedding", pa.list_(pa.float32(), _EMBEDDING_DIMS)),
                pa.array(embeddings, type=pa.list_(pa.float32(), _EMBEDDING_DIMS)),
            )
            break  # one media column per stage (matches derivers._DERIVERS' first-match contract)

    # Same overwrite contract as the in-process compute.transform_stage: enable_stable_row_ids is
    # create-time-only, so a first write creates the target with stable ids and later runs overwrite in
    # place keeping them. A legacy no-stable-id target is migrated once (the tabular path's reset); the
    # media lane's silver dataset is always created BY this contract, so no reset is needed here.
    lance.write_dataset(
        out,
        to_uri,
        mode="overwrite",
        storage_options=so,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )


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

    # Continue the submitting mover's trace (P3): the whole stage transform runs as one child span of
    # the mover's medallion.transform span; without a handed-over context it runs exactly as before.
    with _traced_root("ray.stage_job", {"lance.medallion.stage": stage}):
        _run_stage(from_uri, to_uri, stage, so)


def _run_stage(from_uri: str, to_uri: str, stage: str, so: dict[str, str]) -> None:
    upstream = lance.dataset(from_uri, storage_options=so)

    if _blob_field_names(upstream.schema):
        # MEDIA path: lance_ray strips blob typing on write, so round-trip + derive via pylance (below).
        _media_transform(from_uri, to_uri, so, stage=stage)
    elif "source_rowid" not in upstream.schema.names:
        # CASCADE HEAD (tabular): mint root-provenance source_rowid from the upstream _rowid. lance_ray's
        # distributed read does not surface the reserved _rowid metacolumn, and provenance must be exact, so
        # the head is a native pylance overwrite on the driver (the raw head is small); deeper tabular stages,
        # which already CARRY source_rowid as a plain column, distribute below. Same 2.2 + stable-id contract.
        _reset_if_legacy(to_uri, so)
        lance.write_dataset(
            _stamp_stage(upstream.to_table(with_row_id=True), stage),
            to_uri,
            storage_options=so,
            mode="overwrite",
            data_storage_version="2.2",
            enable_stable_row_ids=True,
        )
    else:
        base = upstream.schema
        if "stage" in base.names:
            base = base.remove(base.get_field_index("stage"))
        out_schema = base.append(pa.field("stage", pa.string()))

        import lance_ray as lr  #   # Ray-image only; lazy (see module top)

        # Distributed transform on Ray, then a stable-row-id write: create dst with stable ids (empty, output
        # schema) and distributed-APPEND the Ray fragments into it (the property is dataset-level, so they
        # inherit it). concurrency>1 → fragments written in parallel + one commit. source_rowid is already a
        # plain column in `base`, so it flows through map_batches + write as ordinary data (no distributed
        # _rowid needed) — only the head, handled natively above, has to mint it.
        transformed = lr.read_lance(from_uri, storage_options=so).map_batches(lambda table: _stamp_stage(table, stage), batch_format="pyarrow")
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
        f"RAY-STAGE OK stage={stage} rows={out.count_rows()} version={out.version} "
        f"dsv={out.data_storage_version} stable_row_ids={out.has_stable_row_ids} cols={out.schema.names}"
    )
    if out.count_rows() != upstream.count_rows() or not out.has_stable_row_ids:
        raise SystemExit("stage transform produced wrong row count or lost stable row ids")


if __name__ == "__main__":
    main()
