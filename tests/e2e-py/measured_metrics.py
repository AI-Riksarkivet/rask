"""Maintenance's own instruments, read through a real in-memory reader while a test drives the worker.

`maintenance.core.metrics` makes its instruments when it is imported, on whatever meter provider is installed then. Its
code is run again into a module object of its own under a meter of this reader's provider, and each instrument is swapped
onto the imported module, so the functions the worker calls (`record_*`, which read the module's globals when called)
count into the reader under the names the alert rules read, and no process-global provider is installed.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Generator, Mapping
from contextlib import contextmanager

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.metrics import Instrument
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader, NumberDataPoint

from maintenance.core import metrics


#: One instrument's points: each point's attributes, as sorted pairs, to its value (a histogram's: its count).
type Points = dict[tuple[tuple[str, str], ...], float]


class Measured:
    """What the instruments hold now, by instrument name."""

    def __init__(self, reader: InMemoryMetricReader) -> None:
        self._reader = reader

    def read(self) -> dict[str, Points]:
        found: dict[str, Points] = {}
        data = self._reader.get_metrics_data()
        for resource in data.resource_metrics if data is not None else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        labels = tuple(sorted((key, str(value)) for key, value in (point.attributes or {}).items()))
                        if isinstance(point, HistogramDataPoint):
                            found.setdefault(metric.name, {})[labels] = float(point.count)
                        elif isinstance(point, NumberDataPoint):
                            found.setdefault(metric.name, {})[labels] = float(point.value)
        return found

    def of(self, name: str, **labels: str) -> float | None:
        """The value of ``name``'s point carrying exactly ``labels`` (a histogram's: its count), or None when it has none."""
        return self.read().get(name, {}).get(tuple(sorted(labels.items())))

    def by(self, name: str, label: str) -> dict[str, float]:
        """``name``'s points by the value of their one label ``label``."""
        return {dict(labels)[label]: value for labels, value in self.read().get(name, {}).items() if [key for key, _ in labels] == [label]}

    def total(self, name: str, **labels: str) -> float | None:
        """The sum of the values a histogram ``name`` recorded on its point carrying exactly ``labels``."""
        data = self._reader.get_metrics_data()
        for resource in data.resource_metrics if data is not None else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points if metric.name == name else ():
                        if isinstance(point, HistogramDataPoint) and dict(point.attributes or {}) == labels:
                            return float(point.sum)
        return None


@contextmanager
def measured(monkeypatch: pytest.MonkeyPatch) -> Generator[Measured]:
    """Every instrument of `maintenance.core.metrics`, counting into a reader of this block's own."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    spec = importlib.util.find_spec(metrics.__name__)
    assert spec is not None and spec.loader is not None, "the metrics module has no loader to run it again with"
    fresh = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(otel_metrics, "get_meter", lambda name, *_args, **_kwargs: provider.get_meter(name))
        spec.loader.exec_module(fresh)
    instruments: Mapping[str, object] = {name: value for name, value in vars(fresh).items() if isinstance(value, Instrument)}
    assert instruments, "the metrics module made no instrument"
    for name, instrument in instruments.items():
        monkeypatch.setattr(metrics, name, instrument)
    try:
        yield Measured(reader)
    finally:
        provider.shutdown()
