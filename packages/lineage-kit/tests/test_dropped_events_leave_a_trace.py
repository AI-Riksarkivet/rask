"""A lineage event this package throws away must be COUNTABLE, and authoring is not transport (PS-23).

`ClientEmitter.emit` was one `try` around two very different operations:

    self._client.emit(event.to_openlineage())

`to_openlineage()` is AUTHORING — turning our own model into the client's. If that raises, the bug is
in the producer's event, not in the network, yet it was logged as `lineage_emit_failed` and became
indistinguishable from a lineage service that was simply down. And in both cases the ONLY trace left
was a log line: no series, so no vmalert rule could ever fire on "this deployment has been emitting
nothing for an hour", which is exactly the 2026-07-13 shape (every training RunEvent 401'd, and the
whole training provenance vanished behind one warning per event).

Emission still never crashes compute — that guarantee is the reason the swallow exists. What changes
is that the swallow now leaves evidence.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest
from openlineage.client import OpenLineageClient

from lineage_kit import ClientEmitter, Job, NoopEmitter, Run, RunEvent, RunState


def _reader() -> Any:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    return reader, MeterProvider(metric_readers=[reader])


def _points(reader: Any, name: str) -> list[Any]:
    out: list[Any] = []
    data = reader.get_metrics_data()
    for rm in getattr(data, "resource_metrics", []) or []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    out.extend(metric.data.data_points)
    return out


def _event() -> RunEvent:
    return RunEvent(
        eventType=RunState.START,
        eventTime="2026-08-29T00:00:00Z",
        run=Run(runId="6f2b1a5e-1f3d-5a0e-9c4b-2f9f0a7d1c33"),
        job=Job(namespace="rask", name="ingest"),
    )


class _DeadClient:
    """A client whose transport is down — the shape of a lineage service that is not answering."""

    def __init__(self) -> None:
        self.calls = 0

    def emit(self, payload: object) -> None:
        self.calls += 1
        raise ConnectionError("connection refused")


class _RecordingClient:
    def __init__(self) -> None:
        self.calls = 0

    def emit(self, payload: object) -> None:
        self.calls += 1


def _as_client(stub: object) -> OpenLineageClient:
    """`ClientEmitter` uses exactly one method of its client. Cast, not `ignore`: the substitution is
    the test's own claim, and a real client would open a transport."""
    return cast(OpenLineageClient, stub)


@pytest.fixture
def metrics_reader():
    from lineage_kit import metrics

    reader, provider = _reader()
    metrics.bind_meter_provider(provider)
    yield reader
    metrics.bind_meter_provider(None)


def test_a_transport_failure_leaves_a_series_not_only_a_log_line(metrics_reader: Any) -> None:
    client = _DeadClient()
    ClientEmitter(_as_client(client)).emit(_event())  # never raises — the guarantee this package makes

    points = _points(metrics_reader, "lineage.events.dropped")
    assert points, "a dropped lineage event left nothing a monitoring rule could fire on"
    assert points[0].value == 1
    assert points[0].attributes["lance.lineage.reason"] == "transport"


def test_an_authoring_failure_is_not_reported_as_a_transport_failure(metrics_reader: Any, caplog: pytest.LogCaptureFixture) -> None:
    """A model that cannot be serialised is OUR bug; a refused connection is the estate's. Not one label."""
    client = _RecordingClient()
    event = _event()
    object.__setattr__(event, "to_openlineage", lambda: (_ for _ in ()).throw(TypeError("unserialisable facet")))

    with caplog.at_level(logging.WARNING, logger="lineage_kit.emitter"):
        ClientEmitter(_as_client(client)).emit(event)

    assert client.calls == 0, "an event that could not be authored was still handed to the transport"
    assert [r.getMessage() for r in caplog.records] == ["lineage_author_failed"], "an authoring bug is logged as a transport failure"

    points = _points(metrics_reader, "lineage.events.dropped")
    assert points and points[0].attributes["lance.lineage.reason"] == "author"


def test_a_healthy_emit_counts_no_drop(metrics_reader: Any) -> None:
    client = _RecordingClient()
    ClientEmitter(_as_client(client)).emit(_event())
    assert client.calls == 1
    assert not _points(metrics_reader, "lineage.events.dropped")


# --------------------------------------------------------------------------- #
# `emit()` STATES the outcome it already computed, so a caller can decide to stage
# --------------------------------------------------------------------------- #
# The swallow stays (a run whose data landed must not be reported failed because the graph was
# unreachable); what the caller gets is the answer. The bool means "this event needs no recovery", not "it
# reached the graph", which is why `NoopEmitter` answers True: a deployment with lineage switched off has
# lost nothing, and staging its events would grow an outbox nothing drains.


def test_a_delivered_event_reports_true() -> None:
    client = _RecordingClient()
    assert ClientEmitter(_as_client(client)).emit(_event()) is True
    assert client.calls == 1, "the happy path stopped emitting"


def test_a_refused_door_reports_false() -> None:
    """The shape of both recorded incidents: the transport refused, the run's data landed anyway, and
    nothing downstream could tell."""
    assert ClientEmitter(_as_client(_DeadClient())).emit(_event()) is False


def test_an_unauthorable_event_reports_false_too() -> None:
    """The OTHER failure mode, which the emitter deliberately keeps distinct: a producer that built an
    unserialisable facet never reaches the transport at all, and is just as lost."""

    class _Unauthorable(RunEvent):
        def to_openlineage(self) -> Any:
            raise ValueError("facet is not serialisable")

    event = _Unauthorable.model_validate(_event().model_dump(by_alias=True))
    client = _RecordingClient()
    assert ClientEmitter(_as_client(client)).emit(event) is False
    assert client.calls == 0, "an unauthorable event reached the transport"


@pytest.mark.parametrize(
    ("emitter", "expected"),
    [(NoopEmitter(), True)],
    ids=["noop"],
)
def test_the_emitters_that_lose_nothing_report_true(emitter: Any, expected: bool) -> None:
    """`NoopEmitter` answers True because the bool means "needs no recovery", not "reached the graph"."""
    assert emitter.emit(_event()) is expected
