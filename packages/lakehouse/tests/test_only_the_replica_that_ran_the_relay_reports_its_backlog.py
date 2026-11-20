"""An outbox's backlog is reported by the replica that measured it on the relay's last window, under its own lane.

The relay's timer runs each tick on one replica (`lakehouse.timers`). A replica that measured a backlog and then loses
the following windows holds a snapshot of a prefix others have since drained; exported, it would page on an outbox
that is empty. The lakehouse keeps two outboxes and one process can work both, so each series names its lane, and each
lane's counters exist at 0 from its start, since an alert reads a counter's increase. The exported series are read
through an OpenTelemetry reader, as the Collector reads them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from lakehouse.events import outbox, outbox_metrics
from lakehouse.storage_options import StorageOptions


type Series = dict[str, dict[str, float]]


def _exported(reader: InMemoryMetricReader) -> Series:
    """Each exported metric's value by lane."""
    data = reader.get_metrics_data()
    points: Series = {}
    for resource in data.resource_metrics if data is not None else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                points[metric.name] = {
                    str((point.attributes or {})["lance.outbox.lane"]): float(point.value)
                    for point in metric.data.data_points
                    if isinstance(point, NumberDataPoint)
                }
    return points


@pytest.fixture
def measuring() -> tuple[ModuleType, InMemoryMetricReader]:
    """The outbox metrics module run again under a provider of the test's own, and the reader that collects from it."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    spec = importlib.util.find_spec(outbox_metrics.__name__)
    assert spec is not None and spec.loader is not None, "the outbox metrics module has no loader to run it again with"
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(otel_metrics, "get_meter", lambda name, *_args, **_kwargs: provider.get_meter(name))
        spec.loader.exec_module(module)
    return module, reader


def test_a_replica_reports_each_lanes_backlog_until_another_runs_the_window_or_it_cannot_measure(measuring: tuple[ModuleType, InMemoryMetricReader]) -> None:
    metrics, reader = measuring

    metrics.observe_backlog("lineage", 3, 400.0)
    metrics.observe_backlog("control", 1, 20.0)
    measured = _exported(reader)
    metrics.forget_backlog("lineage")
    forgotten = _exported(reader)
    metrics.observe_backlog("lineage", 0, 0.0)
    measured_again = _exported(reader)

    assert (measured["outbox.depth"], measured["outbox.oldest_age"]) == ({"lineage": 3.0, "control": 1.0}, {"lineage": 400.0, "control": 20.0})
    assert (forgotten["outbox.depth"], forgotten["outbox.oldest_age"]) == ({"control": 1.0}, {"control": 20.0})
    assert measured_again["outbox.depth"] == {"lineage": 0.0, "control": 1.0}


def test_a_tick_whose_listing_the_store_does_not_answer_reports_no_backlog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What a relay calls each tick measures the real prefix; once the store stops answering the listing, the reading
    it took before says nothing about the outbox, so the lane's series ends where it would have reported 0 throughout."""
    reader = InMemoryMetricReader()
    monkeypatch.setattr(outbox_metrics, "_meter", MeterProvider(metric_readers=[reader]).get_meter("lance.outbox"))
    monkeypatch.setattr(outbox_metrics, "_gauges_registered", False)
    monkeypatch.setattr(outbox_metrics, "_backlogs", {})
    uri = f"file://{tmp_path}/outbox"

    def unanswered(_uri: str, _options: StorageOptions) -> list[object]:
        raise OSError("the store did not answer the listing")

    measured = outbox.measured_backlog("lineage", uri, StorageOptions())
    while_answering = _exported(reader)
    monkeypatch.setattr(outbox, "_staged_infos", unanswered)
    with pytest.raises(OSError, match="did not answer"):
        outbox.measured_backlog("lineage", uri, StorageOptions())

    assert (measured, while_answering["outbox.depth"]) == ((0, 0.0), {"lineage": 0.0})
    assert "outbox.depth" not in _exported(reader)


def test_a_lanes_counters_exist_at_zero_from_its_start_and_count_under_their_lane(measuring: tuple[ModuleType, InMemoryMetricReader]) -> None:
    metrics, reader = measuring

    metrics.create_series("control")
    at_start = _exported(reader)
    metrics.record_publish_failed("control")
    metrics.record_stage_failed("lineage")
    after = _exported(reader)

    counters = {
        "outbox.events.staged",
        "outbox.events.published",
        "outbox.publish.failed",
        "outbox.stage.failed",
        "outbox.drop.failed",
        "outbox.events.drained",
        "outbox.events.stranded",
        "outbox.events.refused",
        "outbox.events.poison_dropped",
    }
    assert at_start == {counter: {"control": 0.0} for counter in counters}
    assert after["outbox.publish.failed"] == {"control": 1.0}
    assert after["outbox.stage.failed"] == {"control": 0.0, "lineage": 1.0}
