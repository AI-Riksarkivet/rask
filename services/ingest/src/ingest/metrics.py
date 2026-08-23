"""Domain metrics for the ingest plane — the service had none at all.

`grep -rn opentelemetry services/ingest/` returned nothing before this module, and
`chart/alerting/rules.yml` duly contains zero ingest rules: no page fires for a run that failed, a
fan-out that stalled, or units that never landed.

Dapr's own workflow families cannot fill the gap, because the run-level `status` label is FALSE for
this codebase. The error boundary converts failure into a RETURNED `RunOutcome(status="FAILED")`
rather than letting the orchestrator raise, so the workflow completes normally and the sidecar records
`status="success"` for a run that died. An alert on that label reads green while every harvest fails.
The verdict is a fact only the application knows.

Bounded labels only: a closed status and a closed outcome pair, both owned by this module's callers.
Run ids, unit ids, dataset names and the `errors` dict stay on spans and logs.
"""

from __future__ import annotations

from opentelemetry import metrics


_meter = metrics.get_meter("lance.ingest")

_runs = _meter.create_counter(
    "ingest.runs",
    unit="{run}",
    description="Ingest runs by terminal status (COMPLETE|FAILED) — the verdict Dapr's own family cannot carry.",
)
_units = _meter.create_counter(
    "ingest.units",
    unit="{unit}",
    description="Units by terminal outcome (written|failed), recorded at the run's terminal activity.",
)


def record_run(status: str) -> None:
    """Count one terminal run by status."""
    _runs.add(1, {"lance.ingest.status": status})


def record_units(*, written: int, failed: int) -> None:
    """Record what the run moved and what it lost, from the one place that knows both.

    `errors_total` rather than `len(errors)` deliberately — the errors dict is CAPPED, so counting its
    entries would under-report exactly on the runs that failed hardest.
    """
    if written:
        _units.add(written, {"lance.ingest.outcome": "written"})
    if failed:
        _units.add(failed, {"lance.ingest.outcome": "failed"})
