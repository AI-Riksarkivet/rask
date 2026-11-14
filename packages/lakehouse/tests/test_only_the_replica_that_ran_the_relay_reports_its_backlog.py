"""The outbox's backlog is reported by the replica that measured it on the relay's last window, and by no other.

The relay's timer runs each tick on one replica (`lakehouse.timers`). A replica that measured a backlog and then loses
the following windows holds a snapshot of a prefix others have since drained; exported, it would page on an outbox
that is empty. The exported series is read through an OpenTelemetry reader, as the Collector reads it.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from lakehouse import outbox_metrics


def _exported(reader: InMemoryMetricReader) -> dict[str, list[float]]:
    data = reader.get_metrics_data()
    points: dict[str, list[float]] = {}
    for resource in data.resource_metrics if data is not None else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                points[metric.name] = [float(point.value) for point in metric.data.data_points if isinstance(point, NumberDataPoint)]
    return points


def test_a_replica_reports_the_backlog_it_measured_until_another_runs_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = InMemoryMetricReader()
    monkeypatch.setattr(outbox_metrics, "_meter", MeterProvider(metric_readers=[reader]).get_meter("lance.outbox"))
    monkeypatch.setattr(outbox_metrics, "_gauges_registered", False)

    outbox_metrics.observe_backlog(3, 400.0)
    measured = _exported(reader)
    outbox_metrics.forget_backlog()
    forgotten = _exported(reader)
    outbox_metrics.observe_backlog(0, 0.0)
    measured_again = _exported(reader)

    assert (measured["outbox.depth"], measured["outbox.oldest_age"]) == ([3.0], [400.0])
    assert "outbox.depth" not in forgotten
    assert "outbox.oldest_age" not in forgotten
    assert (measured_again["outbox.depth"], measured_again["outbox.oldest_age"]) == ([0.0], [0.0])
