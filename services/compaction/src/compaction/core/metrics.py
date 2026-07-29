"""OpenTelemetry domain metrics for the compaction service (exported OTLP-direct to GreptimeDB)."""

from __future__ import annotations

from opentelemetry import metrics


_meter = metrics.get_meter("lance.compaction")

_runs = _meter.create_counter(
    "compaction.runs",
    unit="{run}",
    description="Compaction sweeps triggered by the Dapr cron binding.",
)
_fragments_removed = _meter.create_counter(
    "compaction.fragments.removed",
    unit="{fragment}",
    description="Small Lance fragments merged away by compaction.",
)
_versions_removed = _meter.create_counter(
    "compaction.versions.removed",
    unit="{version}",
    description="Superseded Lance manifest versions GC'd.",
)
_indices_optimized = _meter.create_counter(
    "compaction.indices.optimized",
    unit="{index}",
    description="Secondary indices (vector/scalar/FTS) re-optimized to cover new fragments.",
)


def record_run() -> None:
    _runs.add(1)


def record_reclaimed(fragments_removed: int, versions_removed: int, indices_optimized: int = 0) -> None:
    """Record what one sweep reclaimed + re-optimized across all datasets. Always emit — adding 0 is a valid
    no-op that still CREATES the counter series, so a dashboard/alert on ``rate(compaction_*_total[5m])``
    has data from the first sweep instead of reading "no data" until the first non-zero reclaim (obs audit
    2026-07-13)."""
    _fragments_removed.add(fragments_removed)
    _versions_removed.add(versions_removed)
    _indices_optimized.add(indices_optimized)
