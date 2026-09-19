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


_provenance_missing = _meter.create_gauge(
    "lineage.reconcile.provenance_missing",
    unit="{table}",
    description="Tables whose committed provenance the graph does not hold, as of the last reconcile sweep, by class.",
)


class ProvenanceGap(StrEnum):
    """WHY a committed write's provenance is absent from the graph — the gauge's only attribute.

    Two values, because they are found by different means and answered by different actions.
    """

    #: A GOVERNED table with no dataset node at all. Found by the set difference the sweep's every other
    #: axis cannot reach, since they all enumerate the graph. Nothing repairs it automatically: the
    #: graph holds no `dataSource` URI for a table it has never seen, so a node invented here would
    #: assert a write nobody observed.
    UNKNOWN_TO_GRAPH = "unknown_to_graph"
    #: Versions on disk, below a KNOWN dataset's tip, carrying no WROTE edge. The sweep back-fills these,
    #: and the count still matters: the recovered edge carries ``author='reconcile'`` and no inputs, so
    #: the version's actor and derivation are gone for good. A count that keeps returning on the same
    #: dataset names a producer that is not emitting.
    VERSIONS_BELOW_TIP = "versions_below_tip"


def record_provenance_gaps(*, unknown_to_graph: int | None, versions_below_tip: int) -> None:
    """Publish one sweep's provenance-completeness counts.

    SYNCHRONOUS gauge, for the reason `medallion.services.cascade_lag` already states: an observable
    gauge computes inside an SDK callback, and these numbers come from a graph query and an FGA
    enumeration. IO in a collection callback blocks the exporter and fails invisibly. The cron tick
    already did the reads; this records what it found.

    ``unknown_to_graph=None`` PUBLISHES NO POINT. A gauge has no "unknown", and every sentinel becomes a
    number someone reads: ``0`` is exactly what a healthy estate reports, so publishing it when FGA was
    off or its store unreadable turns a blind sweep into a clean bill of health. Publishing nothing
    leaves the series STALE, which is what a staleness alert is built to notice.
    """
    if unknown_to_graph is not None:
        _provenance_missing.set(unknown_to_graph, {"lance.lineage.provenance_gap": ProvenanceGap.UNKNOWN_TO_GRAPH.value})
    _provenance_missing.set(versions_below_tip, {"lance.lineage.provenance_gap": ProvenanceGap.VERSIONS_BELOW_TIP.value})


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
