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
    # A bus event the stamped subject was not authorized to record (§ E2). Its OWN value rather than
    # `DROPPED`, though both ack the same way: the two send an operator to different places — a refusal
    # is an authorization question about a producer, a drop is a schema question about its payload — and
    # a refusal is the one that can be a silent, deliberate loss, so it gets its own alert.
    REFUSED = "refused"  # authorization denied → Dapr DROP (redelivery cannot grant a permission)
    RETRIED = "retried"  # transient failure → Dapr RETRY (sidecar redelivers)
    DEAD_LETTERED = "dead_lettered"  # exhausted the resiliency schedule → parked, and the graph lacks the run
    # Parked, but the graph ALREADY holds the run — visibility, not loss. Its own value because
    # `DEAD_LETTERED` is read as the terminal-loss signal and a re-park of a recorded run is not loss:
    # the ingest consumer is deliverPolicy=all + ephemeral, so every restart re-reads up to the stream's
    # retention and re-parks whatever still fails, while deterministic run ids mean a stage that runs
    # again heals the gap. Measured against the live estate 2026-09-13: 8,515 parked deliveries of
    # `lineage.events.v1` against a stream whose last sequence was 5,860, and 17 of 21 distinct parked
    # run ids already present in the graph.
    PARKED_ALREADY_RECORDED = "parked_already_recorded"
    # UNREPAIRABLE, and therefore the one outcome that ACKS a run the graph does not hold: a payload
    # that does not parse, or a run carrying no author at all. No tuple, redelivery or restart can
    # change that answer, so parking it writes a dead-letter duplicate on every roll — measured
    # 2026-09-18, 37 of 44 refusals in one hour were one unauthored run id re-presented at pod start.
    #
    # ITS OWN VALUE BECAUSE THE ACK IS A DELIBERATE, SILENT DISCARD. `REFUSED` still means a named
    # person who lacks a grant, which parks and can be repaired by writing the tuple; this one cannot be
    # repaired at all and leaves nothing behind but this count, which is what makes the count the safety
    # argument for acking rather than a statistic. A producer regression shows up here as a rising line
    # and nowhere else.
    UNREPAIRABLE = "unrepairable"


def record_outcome(outcome: Outcome) -> None:
    """Increment the processed-events counter for ``outcome``."""
    _events_processed.add(1, {"lance.lineage.outcome": outcome.value})


def record_ingest_duration(seconds: float) -> None:
    """Record one successful-ingest latency sample."""
    _ingest_duration.record(seconds)
