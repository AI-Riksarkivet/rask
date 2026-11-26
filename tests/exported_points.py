"""A real meter a test reads back: an instrument made on it exports through the SDK, so a test asserts the points and
attributes a Collector would receive.

A module's instruments are made at import on the process's meter, which no test owns, so a test rebinds the one it
drives to an instrument made here, under the name the module gives it, and reads what the module's recorder exported.
"""

from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint


class Exported:
    """One test's meter and what it exported."""

    def __init__(self) -> None:
        self._reader = InMemoryMetricReader()
        self.meter: Meter = MeterProvider(metric_readers=[self._reader]).get_meter("test")

    def points(self, name: str) -> list[tuple[dict[str, object], int | float]]:
        """Every point the instrument ``name`` exported, each its attributes and its value, ordered by attributes."""
        data = self._reader.get_metrics_data()
        found = [
            (dict(point.attributes or {}), point.value)
            for resource in (data.resource_metrics if data is not None else ())
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
            if isinstance(point, NumberDataPoint)
        ]
        return sorted(found, key=lambda point: sorted((key, str(value)) for key, value in point[0].items()))
