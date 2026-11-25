"""What an emitter counts of the events it did not get onto the bus (`lakehouse.events.undelivered`), read on a meter
provider of the test's own: every lane counts on ``<service>.<lane>_emit.failed`` by reason and by whether the outbox
holds the event."""

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from lakehouse.events import undelivered


def counting(monkeypatch: pytest.MonkeyPatch, provider: MeterProvider) -> None:
    """Build every undelivered counter made from now on on ``provider``: an emitter builds its own when it is built."""
    monkeypatch.setattr(undelivered.metrics, "get_meter", lambda name, *_args, **_kwargs: provider.get_meter(name))


def undelivered_counts(reader: InMemoryMetricReader, *, service: str, lane: str) -> dict[tuple[str, str], int]:
    """``<service>.<lane>_emit.failed`` as ``reader`` exports it, by reason and by whether the outbox holds the event."""
    data = reader.get_metrics_data()
    return {
        (str((point.attributes or {})[f"lance.{service}.reason"]), str((point.attributes or {})[f"lance.{service}.staged"])): int(point.value)
        for resource in (data.resource_metrics if data is not None else ())
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == f"{service}.{lane}_emit.failed"
        for point in metric.data.data_points
        if isinstance(point, NumberDataPoint)
    }
