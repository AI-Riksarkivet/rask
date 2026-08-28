"""Ray's two tracing planes, wired from the platform — with no workload in either.

Ray has TWO independent tracing switches, configured in different places, and neither was set:

  * Ray CORE — ``headGroupSpec.rayStartParams: {tracing-startup-hook: "service_kit.ray_tracing:setup_tracing"}``.
    Produces PRODUCER/CONSUMER spans on ``.remote()`` calls. **HEAD ONLY**: the hook is persisted to
    GCS internal KV by ``start_head_processes()`` and every Python process that connects reads it from
    there — that is the whole propagation mechanism, and the same key on a ``workerGroupSpec`` is a
    silent no-op. State the honest ceiling before investing here: upstream documents core tracing as
    Alpha and "no longer under active development", and Ray Data / Train / Tune contribute ZERO spans
    of their own, so the payoff is generic per-task spans named after Ray's internal functions — not
    a dataset or operator span.

  * Ray SERVE — ``RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH=service_kit.ray_tracing:serve_span_processors``.
    Produces proxy → router → replica spans and HONOURS AN INBOUND ``traceparent``, which makes it the
    segment that joins a gateway-originated trace to the model call. It is the higher-value of the two
    and Ray's own monitoring docs never mention it exists.

THE CONTRACTS DIFFER AND MUST NOT BE COPY-PASTED. The core hook takes no arguments and returns
``None`` — it sets the global provider itself. The Serve hook takes no arguments and RETURNS a list of
``SpanProcessor`` — Serve builds the provider. Cross-wiring them fails SOFT: Serve catches a bad
import, logs "the proxy/replica will continue running", and nothing goes unhealthy. The failure mode
is silence, so verify by observing spans in ``opentelemetry_traces``, never by checking health.

``RAY_SERVE_TRACING_SAMPLING_RATIO`` defaults to **0.01** upstream. A ten-request smoke test against
the default produces zero spans and looks broken; the chart sets it explicitly.

NO WORKLOAD NAME LIVES HERE. Identity comes from ``OTEL_SERVICE_NAME`` with a platform default, so an
audio runner, an image one and one nobody has written yet report identically. This module replaces
``runners/htr``'s private ``_init_otel``, which built a provider that produced zero spans (no span is
opened anywhere under ``runners/``) and defaulted its service name to that workload.

No new dependency: the Ray image already ships the OTel SDK and the OTLP/HTTP exporter, because
``packages/ray-cluster-env`` (the deps-only member naming the Ray images' environment) depends on
``service-kit[lancekit]`` and ``.docker/ray-cluster.dockerfile`` installs it — so ``service_kit`` is
importable in every Ray Python process on the cluster.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING


if TYPE_CHECKING:  # pragma: no cover — typing only
    from opentelemetry.sdk.trace import SpanProcessor


log = logging.getLogger(__name__)

#: Platform identity when the chart has not set one. Never a workload.
_DEFAULT_SERVICE_NAME = "ray"


def _span_processor() -> SpanProcessor:
    """One OTLP batch processor, configured entirely from ``OTEL_EXPORTER_OTLP_*``.

    BATCH, never SIMPLE. Ray opens a span on every ``.remote()``, and the hooks Ray ships in its own
    documentation use ``SimpleSpanProcessor``, which exports synchronously — that puts an HTTP
    round-trip on the task-submission hot path of a batch system.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    return BatchSpanProcessor(OTLPSpanExporter())


def setup_tracing() -> None:
    """Ray CORE startup hook. Zero arguments, returns None, sets the global provider.

    Registered via ``rayStartParams`` on the HEAD group only. Deliberately does NOT call
    ``ray.get_runtime_context()``: this runs three lines before the worker is marked connected, so
    ``get_job_id()`` raises there — and Ray stamps ``ray.job_id`` / ``node_id`` / ``task_id`` onto its
    spans itself at span time, so reaching for them here buys nothing and breaks worker startup.

    Idempotent. A Ray job script may already have installed its own provider to continue the
    submitter's trace (``scripts/ray_stage_job.py`` does exactly that), and OTel answers a second
    ``set_tracer_provider`` with a warning rather than an error — so without this check the caller
    would believe it had configured tracing while the first provider stayed in place.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return

    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        # Nowhere to export. Returning quietly keeps the same fail-soft contract every Ray job script
        # already has: a telemetry gap must never stop the work.
        return

    provider = TracerProvider(resource=Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME") or _DEFAULT_SERVICE_NAME}))
    provider.add_span_processor(_span_processor())
    trace.set_tracer_provider(provider)
    log.info("ray_core_tracing_enabled", extra={"service_name": os.environ.get("OTEL_SERVICE_NAME") or _DEFAULT_SERVICE_NAME})


def serve_span_processors() -> list[SpanProcessor]:
    """Ray SERVE exporter hook. Zero arguments, RETURNS the processors — Serve owns the provider.

    A DIFFERENT contract from :func:`setup_tracing`, and the difference is the whole reason both live
    in this module: wiring either import path into the other's env var fails soft and silently.
    """
    return [_span_processor()]
