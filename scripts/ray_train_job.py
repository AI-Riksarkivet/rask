"""Ray TRAIN job for the training workload class (#115b, docs/RAY-TRAIN.md D3+D4).

The trainer consumer submits this via the Ray Jobs REST API (submit-and-ack, D2) — the job then owns
its ENTIRE lifecycle: it emits OpenLineage ``START → RUNNING(progress) → COMPLETE|FAIL`` itself over
the lineage HTTP ingest (no sidecar on Ray pods), reads every feature dataset AT ITS PINNED version
(the trigger resolved the pins at the head — nothing floats here), trains the demo-tier model, and
publishes per the D4 crash-safe order:

1. artifact BYTES first — plain objects under ``<artifact_base>/<token>/`` (token-keyed → a retried
   job overwrites its own paths, idempotent);
2. the REGISTRY record second — ONE Lance commit to ``models$<model>`` (rows per artifact, ``payload``
   = external blob pointer at the plain paths; 2.2 + stable row ids + the artifact base registered as
   the dataset's external-blob base). The commit IS the atomic registration: a crash between (1) and
   (2) leaves orphan files, never a half-registered model. Model version N == Lance version N.

Self-contained by design (baked into the ray image; no ``services/`` imports) — the run-id derivation
mirrors ``common.openlineage.run_id_for`` byte-for-byte and is pinned against it by a unit test.

Env: MODEL FEATURES(json [{dataset,version,uri}]) CONFIG TOKEN MODELS_NAMESPACE REGISTRY_URI
     ARTIFACT_BASE [LINEAGE_URL] [LINEAGE_TOKEN] S3_ENDPOINT S3_KEY S3_SECRET [S3_REGION]
     [TRACEPARENT TRACESTATE OTEL_*] — trace continuity across the Ray boundary (prod-readiness P3):
     when the submitting consumer injected its span + OTLP config, the job runs under one root span
     parented on that trace; absent → untraced, exactly as before.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any


_PRODUCER = "https://github.com/Borg93/lance-ns/tree/main/scripts/ray_train_job.py"
_RUN_SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
_BASE_FACET = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/BaseFacet"
#: Same spec pin as the medallion emitter (services/medallion/schemas/events.py) — equality-pinned by
#: tests/unit/test_train_job.py so the two emitters can never drift on the facet version.
_VERSION_FACET_SCHEMA = "https://openlineage.io/spec/facets/1-0-1/DatasetVersionDatasetFacet.json#/$defs/DatasetVersionDatasetFacet"
#: Mirrors common.openlineage._RUN_ID_NAMESPACE (uuid5 of the project URL under NAMESPACE_URL) —
#: pinned equal by tests/unit/test_train_job.py so the two derivations can never drift.
_RUN_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Borg93/lance-ns")


def run_id_for(seed: str) -> str:
    return str(uuid.uuid5(_RUN_ID_NAMESPACE, seed))


def _storage_options() -> dict[str, str]:
    return {
        "endpoint": os.environ["S3_ENDPOINT"],
        "access_key_id": os.environ["S3_KEY"],
        "secret_access_key": os.environ["S3_SECRET"],
        "region": os.environ.get("S3_REGION", "us-east-1"),
        "allow_http": "true",
        "virtual_hosted_style_request": "false",
    }


def build_event(
    *,
    event_type: str,
    token: str,
    model: str,
    namespace: str,
    features: list[dict[str, Any]],
    registry_uri: str = "",
    version: int | None = None,
    progress: tuple[int, int] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """One spec-true training ``RunEvent`` (D3): official ``jobType=TRAINING`` facet, per-input
    ``DatasetVersionDatasetFacet`` pins, output version on COMPLETE only — a FAIL keeps a
    version-less output and the standard ``errorMessage`` facet, never a fabricated version.
    The output carries ``dataSource`` (the registry URI) on EVERY event type: it is location
    metadata, not a success claim, and it is what lets the lineage reconcile back-fill recover a
    model version whose COMPLETE emit was lost (review 2026-07-11 — without it the models node has
    no source_uri and the B4 sweep can never repair it)."""
    run_facets: dict[str, Any] = {"lance": {"_producer": _PRODUCER, "_schemaURL": _BASE_FACET, "operation": "training", "token": token}}
    if progress is not None:
        run_facets["progress"] = {
            "_producer": _PRODUCER,
            "_schemaURL": _BASE_FACET,
            "done": progress[0],
            "total": progress[1],
        }
    if error is not None:
        run_facets["errorMessage"] = {
            "_producer": _PRODUCER,
            "_schemaURL": "https://openlineage.io/spec/facets/1-0-1/ErrorMessageRunFacet.json#/$defs/ErrorMessageRunFacet",
            "message": error[:1000],
            "programmingLanguage": "PYTHON",
        }
    inputs = [
        {
            # The consumer only forwards validated `stage$name` datasets, so this matches the stage
            # namespace the medallion movers stamp on the SAME graph nodes (never a whole bare name).
            "namespace": feature["dataset"].split("$", 1)[0],
            "name": feature["dataset"],
            "facets": {
                "version": {
                    "_producer": _PRODUCER,
                    "_schemaURL": _VERSION_FACET_SCHEMA,
                    "datasetVersion": str(feature["version"]),
                }
            },
        }
        for feature in features
    ]
    output_facets: dict[str, Any] = {}
    if registry_uri:  # location metadata, on ALL event types — the reconcile back-fill key
        output_facets["dataSource"] = {
            "_producer": _PRODUCER,
            "_schemaURL": "https://openlineage.io/spec/facets/1-0-1/DatasourceDatasetFacet.json#/$defs/DatasourceDatasetFacet",
            "name": registry_uri,
            "uri": registry_uri,
        }
    if version is not None:  # COMPLETE only — the registry commit that just happened
        output_facets["version"] = {
            "_producer": _PRODUCER,
            "_schemaURL": _VERSION_FACET_SCHEMA,
            "datasetVersion": str(version),
        }
        output_facets["schema"] = {
            "_producer": _PRODUCER,
            "_schemaURL": "https://openlineage.io/spec/facets/1-2-0/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet",
            "fields": [
                {"name": "artifact", "type": "string"},
                {"name": "payload", "type": "blob"},
                {"name": "meta", "type": "string"},
            ],
        }
    output: dict[str, Any] = {"namespace": namespace, "name": f"{namespace}${model}"}
    if output_facets:
        output["facets"] = output_facets
    return {
        "eventType": event_type,
        "eventTime": datetime.now(UTC).isoformat(),
        "producer": _PRODUCER,
        "schemaURL": _RUN_SCHEMA,
        "run": {"runId": run_id_for(f"train-{token}"), "facets": run_facets},
        "job": {
            "namespace": "ray-jobs",
            "name": f"train.{model}",
            "facets": {
                "jobType": {
                    "_producer": _PRODUCER,
                    "_schemaURL": "https://openlineage.io/spec/facets/2-0-3/JobTypeJobFacet.json#/$defs/JobTypeJobFacet",
                    "processingType": "BATCH",
                    "integration": "RAY",
                    "jobType": "TRAINING",
                }
            },
        },
        "inputs": inputs,
        "outputs": [output],
    }


def emit(event: dict[str, Any]) -> None:
    """Best-effort POST to the lineage HTTP ingest (Ray pods carry no Dapr sidecar). Two attempts,
    then give up loudly on stderr — provenance must never crash the training itself.

    Under the chart's ``auth.enabled`` the ingest 401s an unauthenticated POST — which silently cost a
    governed deployment ALL of its training provenance until 2026-07-13. We authenticate as the SERVICE
    we already are: ``LINEAGE_SERVICE_TOKEN`` (the app token the estate shares) + ``LINEAGE_SERVICE_ID``
    (the bare FGA subject, ``service-trainer``), which lineage verifies against its allowlist and then
    FGA-checks on the outputs — so the trainer can only record provenance for what D5's rung permits.
    A human/OIDC ``LINEAGE_TOKEN`` bearer is still honoured (external producers, tests).
    An HTTP status in the failure line keeps a 401/403 (credential problem) distinguishable from an outage."""
    url = os.environ.get("LINEAGE_URL", "").rstrip("/")
    if not url:
        return
    headers = {"content-type": "application/json"}
    if service_token := os.environ.get("LINEAGE_SERVICE_TOKEN", ""):
        headers["dapr-api-token"] = service_token
        headers["x-lance-service-identity"] = os.environ.get("LINEAGE_SERVICE_ID", "")
    elif token := os.environ.get("LINEAGE_TOKEN", ""):
        headers["authorization"] = f"Bearer {token}"
    body = json.dumps(event).encode()
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(f"{url}/api/v1/lineage", data=body, headers=headers)
            urllib.request.urlopen(req, timeout=10)  # noqa: S310 — in-cluster service URL from env
            return
        except urllib.error.HTTPError as exc:
            print(f"lineage emit attempt {attempt} rejected: HTTP {exc.code}", file=sys.stderr)
        except Exception as exc:
            print(f"lineage emit attempt {attempt} failed: {exc}", file=sys.stderr)


def emit_metrics(model: str, metrics: dict[str, Any], *, reader: Any = None) -> None:
    """Best-effort export of a run's numeric metrics to OTLP → GreptimeDB (→ Perses; #18, not MLflow).

    The run's params live in the registry meta and the OpenLineage event; the numeric metrics also flow here
    as telemetry so a Perses dashboard can chart them over time. No-op when no OTLP endpoint is configured
    (dev / auth-off). The Ray job is short-lived, so it builds a MeterProvider, records, force-flushes, and
    shuts down inline — a periodic reader alone would drop the final export before the process exits. A
    telemetry failure never fails the training run. Labels are bounded to ``{model}`` (per-run ids stay in
    lineage, not in metric cardinality). ``reader`` is injectable so a test can capture without a real export.
    """
    own_reader = reader is None
    if own_reader and not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource

        if own_reader:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "service-trainer")})
        provider = MeterProvider(metric_readers=[reader], resource=resource)
        meter = provider.get_meter("lance.training")
        labels = {"lance.model": model}
        meter.create_counter("lance.training.runs", description="Completed training runs, by model.").add(1, labels)
        for name, value in metrics.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue  # only real numeric scalars become gauges (a bool/str metric is not chartable)
            meter.create_gauge(f"lance.training.{name}", description=f"Latest training metric {name!r}, by model.").set(value, labels)
        # Only flush + close the reader we own (the real OTLP export); an injected reader is the caller's
        # to collect (a second collect here would consume the gauges' last-value before the caller reads).
        if own_reader:
            provider.force_flush()
            provider.shutdown()
    except Exception as exc:
        print(f"OTLP metrics export failed: {exc}", file=sys.stderr)


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


def train_demo_model(tables: list[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Demo-tier CPU 'training': per-numeric-column means over all pinned features → 'weights', plus
    row-count metrics. Deterministic and dependency-free — the seam a TorchTrainer drops into (D6)."""
    weights: dict[str, float] = {}
    rows = 0
    for table in tables:
        rows += table.num_rows
        for column in table.schema.names:
            if str(table.schema.field(column).type) in ("int64", "int32", "float", "double", "float32"):
                values = [v for v in table.column(column).to_pylist() if v is not None]
                if values:
                    weights[column] = float(sum(values) / len(values))
    return {"kind": "column-means", "weights": weights}, {"rows_seen": rows, "features": len(tables)}


def write_artifacts(artifact_base: str, token: str, files: dict[str, bytes]) -> dict[str, str]:
    """STEP 1 (bytes first): land plain objects under ``<base>/<token>/`` — token-keyed, so a retry
    overwrites its own paths. Returns artifact-name → full URI for the pointer rows."""
    base = artifact_base.rstrip("/")
    uris: dict[str, str] = {}
    if base.startswith("s3://"):
        import pyarrow.fs as pafs

        so = _storage_options()
        scheme, _, host = so["endpoint"].partition("://")
        fs = pafs.S3FileSystem(
            endpoint_override=host,
            access_key=so["access_key_id"],
            secret_key=so["secret_access_key"],
            scheme=scheme or "http",
            region=so["region"],
        )
        for name, payload in files.items():
            path = f"{base.removeprefix('s3://')}/{token}/{name}"
            with fs.open_output_stream(path) as out:
                out.write(payload)
            uris[name] = f"s3://{path}"
    else:  # local path — the unit-test tier
        target = os.path.join(base, token)
        os.makedirs(target, exist_ok=True)
        for name, payload in files.items():
            with open(os.path.join(target, name), "wb") as out:
                out.write(payload)
            uris[name] = os.path.join(target, name)
    return uris


def publish_registry(
    registry_uri: str,
    artifact_base: str,
    artifact_uris: dict[str, str],
    meta: dict[str, Any],
    storage_options: dict[str, str] | None,
) -> int:
    """STEP 2 (the atomic registration): ONE Lance commit of pointer rows into ``models$<model>``.

    First publish CREATES the registry (2.2 + stable row ids — create-time-only — with the artifact
    base registered as the dataset's external-blob base, which means the base must stay STABLE per
    model); later publishes APPEND. Returns the new Lance version — the model version.

    The existence probe alone decides create-vs-append (review 2026-07-11: a wider try/except here
    misclassified append-time failures as "first publish" and masked the real error behind a
    'Dataset already exists' from the fallback create). A create that loses the concurrent
    first-publish CAS race converges as an append instead of terminally failing the run.
    """
    import lance
    import pyarrow as pa
    from lance.blob import Blob, blob_array
    from lance.dataset import DatasetBasePath

    names = sorted(artifact_uris)
    table = pa.table(
        {
            "artifact": pa.array(names, pa.string()),
            "payload": blob_array([Blob.from_uri(artifact_uris[n]) for n in names]),
            "meta": pa.array([json.dumps(meta)] * len(names), pa.string()),
        }
    )

    def _append() -> int:
        return int(lance.write_dataset(table, registry_uri, mode="append", storage_options=storage_options).version)

    try:  # scope: ONLY the existence probe — a failing append must surface its own error
        lance.dataset(registry_uri, storage_options=storage_options)
    except (ValueError, OSError):
        pass  # not found → first publish
    else:
        return _append()
    try:
        return int(
            lance.write_dataset(
                table,
                registry_uri,
                data_storage_version="2.2",
                enable_stable_row_ids=True,
                initial_bases=[DatasetBasePath(artifact_base.rstrip("/") + "/")],
                storage_options=storage_options,
            ).version
        )
    except OSError as exc:
        if "already exists" not in str(exc).lower():
            raise
        return _append()  # lost the concurrent first-create race — converge as version 2


def main() -> None:
    # MODEL + TOKEN are the run identity — without them there is nothing to attribute an event to,
    # so only these two may hard-crash. Everything else parses under the FAIL guard: a malformed
    # FEATURES/CONFIG or missing S3 env becomes an attributable FAILed run, not a silent vanish
    # (review 2026-07-11 — the consumer already acked; lineage is the only trace left).
    model = os.environ["MODEL"]
    token = os.environ["TRAIN_TOKEN"]  # NOT "TOKEN" — a bare TOKEN env is consumed by lance's object-store
    # env fallback as the AWS session token (bogus x-amz-security-token → RustFS 500) — live 2026-07-13.

    # Continue the submitting consumer's trace (P3): the whole training run is one child span of the
    # submitting trace (a FAIL below re-raises through the span, marking it ERROR before the flush);
    # without a handed-over context it runs exactly as before.
    with _traced_root("ray.train_job", {"lance.model": model}):
        _run_train(model, token)


def _run_train(model: str, token: str) -> None:
    namespace = os.environ.get("MODELS_NAMESPACE", "models")
    registry_uri = os.environ.get("REGISTRY_URI", "")
    features: list[dict[str, Any]] = []

    def event(**kw: Any) -> dict[str, Any]:
        return build_event(
            token=token,
            model=model,
            namespace=namespace,
            features=features,
            registry_uri=registry_uri,
            **kw,
        )

    try:
        features.extend(json.loads(os.environ["FEATURES"]))
        config: dict[str, Any] = json.loads(os.environ.get("CONFIG", "{}"))
        registry_uri = registry_uri or os.environ["REGISTRY_URI"]  # required — KeyError FAILs above
        artifact_base = os.environ["ARTIFACT_BASE"]
        so = _storage_options() if registry_uri.startswith("s3://") else None
    except Exception as exc:
        emit(event(event_type="FAIL", error=f"train config: {exc}"))
        raise

    emit(event(event_type="START"))
    try:
        import lance

        tables = []
        for index, feature in enumerate(features, start=1):
            ds = lance.dataset(feature["uri"], version=feature["version"], storage_options=so)
            tables.append(ds.to_table())
            emit(event(event_type="RUNNING", progress=(index, len(features) + 1)))
        weights, metrics = train_demo_model(tables)
        artifact_uris = write_artifacts(
            artifact_base,
            token,
            {"weights.json": json.dumps(weights).encode(), "metrics.json": json.dumps(metrics).encode()},
        )
        emit(event(event_type="RUNNING", progress=(len(features) + 1, len(features) + 1)))
        meta = {"config": config, "features": features, "metrics": metrics, "token": token}
        version = publish_registry(registry_uri, artifact_base, artifact_uris, meta, so)
        emit(event(event_type="COMPLETE", version=version))
        emit_metrics(model, metrics)  # telemetry → OTLP → GreptimeDB → Perses (best-effort)
        print(f"model {namespace}${model} published at registry version {version}")
    except Exception as exc:
        emit(event(event_type="FAIL", error=f"train: {exc}"))
        raise


if __name__ == "__main__":
    main()
