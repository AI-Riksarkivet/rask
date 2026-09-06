"""A tick that completes records that it completed, on whichever lane it ran.

`record_run_started` lives in `plan_sweep`, which BOTH lanes call; `record_run` lived only in
`run_sweep`, which only the serial lane calls. So on the queue lane — the one every deployment
actually runs — the pair opened and never closed.

MEASURED on the deployed estate 2026-09-06, against GreptimeDB:

    compaction_runs_started_total     54
    compaction_datasets_swept_total   7937
    compaction_runs_total             ABSENT
    absent(compaction_runs_total)     1

That last line is `MaintenanceSweepMetricsMissing` (severity critical, `chart/alerting/rules.yml:339`)
firing, and it has been firing for as long as the queue lane has been deployed. Its own annotation
says "the sweep pod is gone or its OTLP export is broken" — neither is true; 7 937 datasets were
swept. Meanwhile `MaintenanceSweepNotCompleting` (:320) is `sum(increase(compaction_runs_total[30m])) == 0`,
which over a vector that does not exist yields nothing, so the alert that would catch a REAL stall can
never fire at all. One permanently-wrong critical page, and one permanently-silent real one.

The rules' own note reads "compaction_runs_started_total, whose excess over compaction_runs_total is
the lost-pass count" — so the estate reads as 54 lost passes while losing none.

WHAT COMPLETION MEANS ON THE QUEUE LANE, because it is not the same event. The serial tick completes
when every dataset has been maintained. The queue tick completes when the estate has been PLANNED and
the units are durably published — the units themselves are executed later by subscriptions, each
acking for itself. That is the right thing to count here: both alerts ask "is the sweep still
running at all", and on this lane the planner IS the sweep. Counting unit execution instead would
make one tick emit hundreds of completions and break `started`'s pairing with it.
"""

from __future__ import annotations

from typing import cast

import pytest

from maintenance.api import routes
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import MaintenanceEmitter


class _Publisher:
    def __init__(self) -> None:
        self.published: list[dict[str, object]] = []

    async def publish_event(self, **kwargs: object) -> None:
        self.published.append(kwargs)


@pytest.mark.asyncio
async def test_the_queue_lane_closes_the_pair_the_planner_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[int] = []
    completed: list[int] = []

    monkeypatch.setattr(routes, "plan_sweep", lambda settings: (started.append(1), ([], []))[1])
    monkeypatch.setattr(routes, "record_run", lambda: completed.append(1))

    async def _no_units(*args: object, **kwargs: object) -> tuple[int, list[object]]:
        return 0, []

    async def _no_lineage(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(routes, "enqueue_units", _no_units)
    monkeypatch.setattr(routes, "emit_sweep_lineage", _no_lineage)

    class _S:
        work_topic = "maintenance.work.v1"
        work_pubsub = "maintenance-pubsub"
        publish_timeout_seconds = 5.0
        delimiter = "$"

    class _Emitter:
        """Never reached — `emit_sweep_lineage` is stubbed above — but the route's signature names the
        protocol, so a bare `object()` is a type error rather than a shortcut."""

        def emit_maintenance(self, *args: object, **kwargs: object) -> None:
            return None

    summary = await routes.on_cron(
        cast(MaintenanceSettings, _S()),
        cast(MaintenanceEmitter, _Emitter()),
        _Publisher(),
    )

    assert summary["status"] == "enqueued", summary
    assert completed == [1], (
        "the queue lane planned and enqueued but recorded no completion — `compaction_runs_total` "
        "stays absent, so MaintenanceSweepMetricsMissing pages forever and MaintenanceSweepNotCompleting "
        "can never fire"
    )
