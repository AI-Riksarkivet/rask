"""Ray job entrypoint for the DUMMY silver hop (A11).

A script baked into the ray-lance image, exactly like ray_stage_job.py — jobs are artefacts, never
code shipped at submit time, so a transform change is an image rebuild and reproducible by
construction.

The transform itself lives in the SEALED `runners/dummy` project rather than here, so the job body
stays a thin parameterised shim and the mechanics it exercises are unit-tested outside the cluster.
That split is the point: this file is how the cluster invokes it; the runner is what is tested.

Env: FROM_URI TO_URI [BASE_VERSION] [RUN_ID] [LINEAGE_JSON]
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from typing import Any

# Suppressed, and NOT a missing dependency: `dummy_runner` lives in `runners/dummy`, a SEALED model
# environment matched by no uv workspace glob on purpose (root `pyproject.toml` — the heavy model pins
# must never enter the fleet's resolution). It is therefore unresolvable from the root environment BY
# DESIGN and will stay that way; the module is on PATH only inside the Ray image that ships it.
# Suppressed rather than "fixed", because the only fix would be adding `runners/*` to the workspace —
# the exact thing the seal exists to prevent.
from dummy_runner.job import main  # ty: ignore[unresolved-import]


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


if __name__ == "__main__":
    # THE SMOKE LANE MUST PROVE THE PROPERTY IT SMOKE-TESTS. `ray_submit` injects TRACEPARENT into
    # this lane's runtime_env exactly as it does for the production stage lane, and this script used
    # to discard it — so the estate's own GPU-free end-to-end prover ran untraced, and could not
    # demonstrate the one thing the production lanes depend on. Same shape as ray_stage_job's root:
    # continued when a valid context was handed over, never fabricated.
    with _traced_root("ray.dummy_job", {"lance.medallion.stage": os.environ.get("STAGE", "")}):
        sys.exit(main())
