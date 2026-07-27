"""OpenTelemetry domain metrics for the #4 transactional lineage outbox (OTLP-direct to GreptimeDB).

WHY (docs/DECISIONS.md P1.1, the four signals): the outbox is an external boundary (S3 + pub/sub) that
carried ZERO of the four golden signals — a direct violation of
`python-infrastructure/references/observability.md`, which requires
them "for every external boundary (HTTP, DB, queue, cache)". Concretely: every durability property #4
claims was **unobservable**. An outbox that silently stops draining — the one failure that loses lineage
forever — looked exactly like an outbox that is working. The audit ranked this the highest-ROI fix.

The signals, mapped to the four:
  · Traffic  — staged / published / dropped counters (the happy path's rate)
  · Errors   — publish_failed (event left staged = the crash window doing its job) + poison_dropped
  · Saturation — DEPTH and OLDEST-AGE gauges: the alertable pair. Depth > 0 sustained means the relay is
    not draining; oldest-age bounds how much lineage is at risk. These are what a human is actually paged on.
  · Latency  — the publish itself is already covered by the Dapr sidecar's own spans.

Bounded cardinality (observability.md): NO run_id / dataset / token labels — those are unbounded and belong
on spans and logs, not metric attributes. These series carry no attributes at all.
"""

from __future__ import annotations

from opentelemetry import metrics


_meter = metrics.get_meter("lance.outbox")

_staged = _meter.create_counter(
    "outbox.events.staged",
    unit="{event}",
    description="Lineage events written to the object-store outbox BEFORE the publish (the durable copy).",
)
_published = _meter.create_counter(
    "outbox.events.published",
    unit="{event}",
    description="Lineage events whose publish was acked (the staged copy is then dropped).",
)
_publish_failed = _meter.create_counter(
    "outbox.publish.failed",
    unit="{event}",
    description=(
        "Publishes that failed AFTER staging — the event survives on object storage for the reconcile "
        "relay. Non-zero is the crash window working as designed, NOT data loss; sustained non-zero with a "
        "rising depth is the real alarm."
    ),
)
_drained = _meter.create_counter(
    "outbox.events.drained",
    unit="{event}",
    description="Staged survivors the reconcile relay re-ingested into the graph + durable feed.",
)
_poison_dropped = _meter.create_counter(
    "outbox.events.poison_dropped",
    unit="{event}",
    description=(
        "Unparseable staged objects discarded so they cannot wedge the drain. Any non-zero value is a BUG "
        "upstream (a malformed event was written) and warrants investigation — it is a silent lineage loss."
    ),
)

# --- Saturation: the alertable pair. An ObservableGauge is the right instrument (observability.md: "pool/
# queue depth ... ObservableGauge for resource usage") — the relay OBSERVES the prefix on each tick rather
# than the outbox trying to track a distributed count itself. Registered LAZILY on the first backlog
# observation: the producers/movers import this module too (for the counters), and import-time gauges made
# all five of those pods export a constant depth=0/oldest_age=0 forever, diluting any unfiltered
# aggregation over the one series that carries truth — the relay's (audit 2026-07-15).
_depth: float = 0.0
_oldest_age_seconds: float = 0.0
_gauges_registered = False


def _observe_depth(_options: metrics.CallbackOptions) -> list[metrics.Observation]:
    return [metrics.Observation(_depth)]


def _observe_oldest_age(_options: metrics.CallbackOptions) -> list[metrics.Observation]:
    return [metrics.Observation(_oldest_age_seconds)]


def _register_gauges() -> None:
    global _gauges_registered
    if _gauges_registered:
        return
    _gauges_registered = True
    _meter.create_observable_gauge(
        "outbox.depth",
        callbacks=[_observe_depth],
        unit="{event}",
        description=(
            "Events currently staged and undrained. THE alert signal: sustained > 0 means the reconcile "
            "relay is not draining and lineage is accumulating at risk."
        ),
    )
    _meter.create_observable_gauge(
        "outbox.oldest_age",
        callbacks=[_observe_oldest_age],
        unit="s",
        description=(
            "Age of the OLDEST staged event. Bounds how much lineage is at risk and how long the relay has "
            "been failing — depth alone cannot distinguish a brief spike from a stuck relay."
        ),
    )


def record_staged() -> None:
    _staged.add(1)


def record_published() -> None:
    _published.add(1)


def record_publish_failed() -> None:
    _publish_failed.add(1)


def record_drained(count: int) -> None:
    """Always emit — adding 0 is a valid no-op that still CREATES the series, so a dashboard/alert has data
    from the first sweep instead of reading "no data" until the first non-zero drain (the same lesson the
    compaction metrics learned in the 2026-07-13 obs audit)."""
    _drained.add(count)


def record_poison_dropped() -> None:
    _poison_dropped.add(1)


def observe_backlog(depth: int, oldest_age_seconds: float) -> None:
    """Publish the saturation snapshot the relay measured this tick.

    Called on EVERY reconcile tick — including a tick that finds an EMPTY outbox, which is what makes
    ``depth`` fall back to 0 instead of going stale at its last non-zero reading. A gauge that only reports
    when non-zero would alert forever after a single transient spike.
    """
    _register_gauges()  # first observation = the measuring process; only it exports the series
    global _depth, _oldest_age_seconds
    _depth = float(depth)
    _oldest_age_seconds = float(oldest_age_seconds)
