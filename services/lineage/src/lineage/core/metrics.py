"""OpenTelemetry domain metrics for the lineage ingest path.

Auto-instrumentation gives generic HTTP server/client metrics; these are the *business* golden
signals you'd actually alert on — how many lineage events we ingest vs. drop vs. retry, and how long
ingest takes. They go out via the OTel SDK (activated by ``opentelemetry-instrument``) **OTLP-direct to
GreptimeDB** (no Collector — mirrors rask), queryable in PromQL / Perses.

Cardinality is bounded on purpose (the otel skill's #1 cost driver): the only attribute is the bounded,
namespaced ``lance.lineage.outcome`` — per-run / per-table identifiers belong on spans and logs, never on
metric attributes. (Custom attributes use the project's dot-namespaced `lance.*` convention —
deliberately NOT the otel skill's reverse-DNS letter, pinned in todo_fable; in PromQL the dots become
underscores → ``lance_lineage_outcome``.)
``metrics.get_meter`` returns a proxy that binds lazily, so creating the instruments at import time
(before ``opentelemetry-instrument`` installs the real MeterProvider) is safe.
"""

from __future__ import annotations

from enum import StrEnum

from opentelemetry import metrics


_meter = metrics.get_meter("lance.lineage")

_events_processed = _meter.create_counter(
    "lineage.events.processed",
    unit="{event}",
    description="Lineage events processed by the Dapr subscriber, by outcome.",
)
_ingest_duration = _meter.create_histogram(
    "lineage.ingest.duration",
    unit="s",
    description="Wall-clock seconds to ingest one event into the AGE graph (successful ingests only).",
    # Second-scale bucket boundaries. WITHOUT this the SDK default explicit buckets apply, and those are
    # millisecond-tuned (0, 5, 10, 25, … 10000) — a real ~10ms–2s ingest lands entirely in the first [0,5s]
    # bucket, so histogram_quantile() (the p95 panel) reads a flat ~5 for every quantile (obs audit
    # 2026-07-13). The advisory is honoured by the SDK when no View overrides the instrument.
    explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)


class Outcome(StrEnum):
    """The bounded set of terminal outcomes for one delivered event (the only metric attribute)."""

    INGESTED = "ingested"  # graph write committed → Dapr SUCCESS
    DROPPED = "dropped"  # malformed payload → Dapr DROP (redelivery can't fix it)
    RETRIED = "retried"  # transient failure → Dapr RETRY (sidecar redelivers)
    DEAD_LETTERED = "dead_lettered"  # exhausted the resiliency schedule → parked (terminal loss signal)


def record_outcome(outcome: Outcome) -> None:
    """Increment the processed-events counter for ``outcome``."""
    _events_processed.add(1, {"lance.lineage.outcome": outcome.value})


def record_ingest_duration(seconds: float) -> None:
    """Record one successful-ingest latency sample."""
    _ingest_duration.record(seconds)
