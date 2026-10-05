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

That one commit carries the run's COMMIT MARKER (CP-029, `service_kit.lakehouse.commit_marker`): the run's id
(`RASK_IDEMPOTENCY_KEY`, the producer's plan and the Ray submission id) in its transaction properties, so the
producer can tell from the registry's Lance history alone whether a run whose job vanished published its model.
After its own terminal event LANDED, the job reports that terminal to the producer's outcome door
(`RASK_OUTCOME_URL`); a job whose terminal emit failed reports nothing, and the producer's plan sweep records the
terminal instead, so a lost emit never leaves a run without one.

No ``services/`` imports — this is baked into the ray image and must not reach the fleet. It DOES
import ``lineage_kit``, which is not a service: ``packages/ray-cluster-env`` declares it precisely so
the compute plane can emit through one authority (LIN-001, owner ruling 2026-09-18), and its run-id
namespace is byte-identical to the one this file used to derive itself.
Its storage options and artifact filesystem come from ``service_kit.lakehouse.objectfs``, which that
env declares too.

Env, in the `WorkOrder` vocabulary the producer's order serializes (`service_kit.lakehouse.work_order.to_env`, CP-044):
     RASK_PARAM_MODEL RASK_TOKEN RASK_PARAM_FEATURES(json [{dataset,version,uri}]) RASK_PARAM_CONFIG
     RASK_PARAM_ARTIFACT_BASE RASK_DEST_URI (the registry) RASK_STAGE (the models namespace)
     [RASK_ORIGINATOR RASK_PROJECT RASK_IDEMPOTENCY_KEY RASK_OUTCOME_URL]
     From the pod, never the submission: RASK_LINEAGE_ENDPOINT (the lineage ingest), S3_ENDPOINT S3_KEY S3_SECRET
     [S3_REGION]. [TRACEPARENT TRACESTATE OTEL_*] — trace continuity across the Ray boundary (prod-readiness P3):
     when the submitting consumer injected its span + OTLP config, the job runs under one root span parented on that
     trace; absent → untraced.
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
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from lineage_kit import build_emitter, run_id_for
from lineage_kit.schemas import (
    Dataset,
    DatasetFacets,
    DatasetVersionFacet,
    DatasourceFacet,
    ErrorMessageRunFacet,
    Job,
    JobTypeJobFacet,
    OutputDataset,
    Run,
    RunEvent,
    RunFacets,
    RunState,
    SchemaDatasetFacet,
    SchemaField,
    custom_facet,
)
from service_kit.lakehouse.commit_marker import CommitMarker
from service_kit.lakehouse.objectfs import StorageOptions, lance_storage_options, s3_filesystem
from service_kit.lakehouse.run_outcomes import OutcomeReport, report_outcome


#: The lane's own job facet. Everything ELSE about the envelope — the producer URI, every
#: `_schemaURL`, the wire form and the run-id derivation — comes from `lineage-kit`, which is in this
#: image's environment because `packages/ray-cluster-env` declares it. This file was the estate's
#: LAST hand-rolled OpenLineage authority, and a hand-rolled copy is not a style problem: a
#: `_schemaURL` is what a consumer follows to VALIDATE a custom facet, so a stale one fails at the
#: consumer about a producer it cannot name. Both hand-written copies had drifted to `2-0-3` on this
#: very facet against the client's `2-0-4`, agreeing with each other and with nothing authoritative.
#: DUMPED, not held as a model: `Job.facets` is a raw pass-through bag by design (the consumer
#: reads entries this estate does not model), so a model left in it reaches the wire unserialised.
_JOB_TYPE_FACET = JobTypeJobFacet(processingType="BATCH", integration="RAY", jobType="TRAINING").model_dump(by_alias=True)


def _storage_options() -> StorageOptions:
    """The estate's builder: ``allow_http`` follows the endpoint's scheme, and the ``aws_``-prefixed
    credential keys displace an ambient AWS_* environment rather than blending with it."""
    return lance_storage_options(
        os.environ["S3_ENDPOINT"],
        os.environ["S3_KEY"],
        os.environ["S3_SECRET"],
        os.environ.get("S3_REGION", "us-east-1"),
    )


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
    originator: str = "",
    project: str = "",
) -> RunEvent:
    """One spec-true training ``RunEvent`` (D3): official ``jobType=TRAINING`` facet, per-input
    ``DatasetVersionDatasetFacet`` pins, output version on COMPLETE only — a FAIL keeps a
    version-less output and the standard ``errorMessage`` facet, never a fabricated version.
    The output carries ``dataSource`` (the registry URI) on EVERY event type: it is location
    metadata, not a success claim, and it is what lets the lineage reconcile back-fill recover a
    model version whose COMPLETE emit was lost (review 2026-07-11 — without it the models node has
    no source_uri and the B4 sweep can never repair it).

    The MODEL is `lineage-kit`'s; only the lane's own policy is here. ``custom_facet`` stamps the
    producer and base-facet URL so a custom facet stays spec-legal without this file naming either.
    """
    lance: dict[str, Any] = {"operation": "training", "token": token}
    # WHO the run is for, and WHICH tenant's watchers should hear about it. Not `author`: this job
    # authenticates to the lineage ingest as `service-trainer`, and `enforce_author` OVERWRITES the
    # author facet with that verified service sub — deliberately, since honouring a producer-supplied
    # author would let any producer file a row in a named person's inbox. `originator` is the field for
    # a run authored by a service but run FOR a person; without these two keys a training FAIL is
    # dropped by `notifiable()` at "no verified author" and reaches nobody at all.
    if originator:
        lance["originator"] = originator
    if project:
        lance["project"] = project

    # `progress` rides as an extra facet rather than a declared one: it is this lane's, not the
    # estate's, and `RunFacets` keeps unknown keys and passes them to the wire verbatim. Set at
    # CONSTRUCTION because pydantic refuses an attribute the model does not declare after the fact.
    extra = {"progress": custom_facet(done=progress[0], total=progress[1])} if progress is not None else {}
    facets = RunFacets(lance=custom_facet(**lance), **extra)
    if error is not None:
        facets.error_message = ErrorMessageRunFacet(message=error[:1000], programmingLanguage="PYTHON")

    inputs = [
        Dataset(
            # The consumer only forwards validated `stage$name` datasets, so this matches the stage
            # namespace the medallion stage runners stamp on the SAME graph nodes (never a whole bare name).
            namespace=feature["dataset"].split("$", 1)[0],
            name=feature["dataset"],
            facets=DatasetFacets(version=DatasetVersionFacet(datasetVersion=str(feature["version"]))),
        )
        for feature in features
    ]

    output = OutputDataset(namespace=namespace, name=f"{namespace}${model}")
    if registry_uri:  # location metadata, on ALL event types — the reconcile back-fill key
        output.facets.data_source = DatasourceFacet(name=registry_uri, uri=registry_uri)
    if version is not None:  # COMPLETE only — the registry commit that just happened
        output.facets.version = DatasetVersionFacet(datasetVersion=str(version))
        output.facets.schema_ = SchemaDatasetFacet(
            fields=[
                SchemaField(name="artifact", type="string"),
                SchemaField(name="payload", type="blob"),
                SchemaField(name="meta", type="string"),
            ]
        )

    return RunEvent(
        eventType=RunState(event_type),
        eventTime=datetime.now(UTC).isoformat(),
        run=Run(runId=run_id_for(f"train-{token}"), facets=facets),
        job=Job(namespace="ray-jobs", name=f"train.{model}", facets={"jobType": _JOB_TYPE_FACET}),
        inputs=inputs,
        outputs=[output],
    )


def emit(event: RunEvent) -> bool:
    """Send the event to the lineage ingest and answer whether it landed. Best-effort — provenance must never crash
    the training, and a terminal that did not land is not reported to the producer, whose sweep records it instead.

    THE CREDENTIAL RULES ARE `lineage-kit`'s, not this file's. It takes the endpoint from the head's
    ``RASK_LINEAGE_ENDPOINT`` (the pod's, never the submission's) and presents the head's projected
    `rask-lineage` ServiceAccount token, re-read on every emit, so the job reports as the subject the
    lineage door maps that account to: `service-trainer` ([[LH-220]]).
    """
    if not build_emitter().emit(event):
        print(f"lineage emit failed for {event.job.name}", file=sys.stderr)
        return False
    return True


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

    The producer's work order carries its active span (`WorkOrder.observability`), which reaches the job as a
    TRACEPARENT env var in its runtime_env. Absent, malformed, or opentelemetry unimportable
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
        fs = s3_filesystem(_storage_options())
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
    marker: CommitMarker | None = None,
) -> int:
    """STEP 2 (the atomic registration): ONE Lance commit of pointer rows into ``models$<model>``.

    First publish CREATES the registry (2.2 + stable row ids — create-time-only — with the artifact
    base registered as the dataset's external-blob base, which means the base must stay STABLE per
    model); later publishes APPEND. Returns the new Lance version — the model version.

    The existence probe alone decides create-vs-append (review 2026-07-11: a wider try/except here
    misclassified append-time failures as "first publish" and masked the real error behind a
    'Dataset already exists' from the fallback create). A create that loses the concurrent
    first-publish CAS race converges as an append instead of terminally failing the run.

    ``marker`` rides the commit's transaction properties on every path (create, append and the race's append alike;
    both modes take them, measured on pylance 12.0.0 2026-10-05). This commit is the run's ONLY write to the
    registry, so a marker found in the registry's history means the whole registration landed.
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

    properties = marker.properties() if marker is not None else None

    def _append() -> int:
        return int(lance.write_dataset(table, registry_uri, mode="append", storage_options=storage_options, transaction_properties=properties).version)

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
                transaction_properties=properties,
            ).version
        )
    except OSError as exc:
        if "already exists" not in str(exc).lower():
            raise
        return _append()  # lost the concurrent first-create race — converge as version 2


def main() -> None:
    # THE MODEL AND THE TOKEN are the run identity — without them there is nothing to attribute an event to,
    # so only these two may hard-crash. Everything else parses under the FAIL guard: a malformed
    # features/config or missing S3 env becomes an attributable FAILed run, not a silent vanish
    # (review 2026-07-11 — the consumer already acked; lineage is the only trace left).
    model = os.environ["RASK_PARAM_MODEL"]
    token = os.environ["RASK_TOKEN"]  # never a bare TOKEN: lance's object-store env fallback reads it as the AWS session token

    # Continue the submitting consumer's trace (P3): the whole training run is one child span of the
    # submitting trace (a FAIL below re-raises through the span, marking it ERROR before the flush);
    # without a handed-over context it runs exactly as before.
    # THE RUN'S ONE NAME AND ITS DOOR (CP-029). A submission with no id marks nothing; one with no door reports nothing,
    # and the producer's plan sweep resolves the run from Ray's state and the registry's history.
    action_id = os.environ.get("RASK_IDEMPOTENCY_KEY", "").strip()
    marker = CommitMarker(action_id=action_id, run_id=run_id_for(f"train-{token}")) if action_id else None
    outcome_url = os.environ.get("RASK_OUTCOME_URL", "").strip()
    with _traced_root("ray.train_job", {"lance.model": model}):
        _run_train(model, token, marker=marker, outcome_url=outcome_url)


def _terminal(event: RunEvent, outcome_url: str, report: OutcomeReport) -> None:
    """Emit the run's terminal event, then report it to the producer's door, but only when the event landed."""
    if emit(event) and outcome_url:
        report_outcome(outcome_url, report)


def _run_train(model: str, token: str, *, marker: CommitMarker | None = None, outcome_url: str = "") -> None:
    namespace = os.environ.get("RASK_STAGE", "models")
    registry_uri = os.environ.get("RASK_DEST_URI", "")
    features: list[dict[str, Any]] = []

    def event(**kw: Any) -> RunEvent:
        return build_event(
            token=token,
            model=model,
            namespace=namespace,
            features=features,
            registry_uri=registry_uri,
            # Read HERE, so every emit below carries them — including the config-parse FAIL, which is
            # the earliest thing that can go wrong and the one a person most needs to hear about.
            originator=os.environ.get("RASK_ORIGINATOR", ""),
            project=os.environ.get("RASK_PROJECT", ""),
            **kw,
        )

    try:
        features.extend(json.loads(os.environ["RASK_PARAM_FEATURES"]))
        config: dict[str, Any] = json.loads(os.environ.get("RASK_PARAM_CONFIG", "{}"))
        registry_uri = registry_uri or os.environ["RASK_DEST_URI"]  # required — KeyError FAILs above
        artifact_base = os.environ["RASK_PARAM_ARTIFACT_BASE"]
        so = _storage_options() if registry_uri.startswith("s3://") else None
    except Exception as exc:
        error = f"train config: {exc}"
        _terminal(event(event_type="FAIL", error=error), outcome_url, OutcomeReport(status="failed", error=error[:4000]))
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
        version = publish_registry(registry_uri, artifact_base, artifact_uris, meta, so, marker=marker)
    except Exception as exc:
        error = f"train: {exc}"
        _terminal(event(event_type="FAIL", error=error), outcome_url, OutcomeReport(status="failed", error=error[:4000]))
        raise
    _terminal(event(event_type="COMPLETE", version=version), outcome_url, OutcomeReport(status="succeeded", committed_version=version))
    emit_metrics(model, metrics)  # telemetry → OTLP → GreptimeDB → Perses (best-effort)
    print(f"model {namespace}${model} published at registry version {version}")


if __name__ == "__main__":
    main()
