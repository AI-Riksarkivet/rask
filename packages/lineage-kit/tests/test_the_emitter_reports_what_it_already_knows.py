"""`Emitter.emit()` knows the event was dropped and the caller cannot ask.

the lakehouse register, row E1 (drained 2026-09-10; in git history) ("lost origination events are unrecoverable and invisible"), whose
measurement narrowed the clause to ONE service: four of the five lakehouse producers stage durably
through `service_kit.lakehouse.outbox`, and `ingest` has zero outbox usage and emits bare.

THE SWALLOW IS RIGHT AND THE SILENCE IS NOT. `ClientEmitter` catches both failure modes on purpose —
`ingest/lineage.py::_emit` states why: *"A run whose data landed must not be reported as failed because
the graph was unreachable — that would turn an observability outage into a data incident."* That
reasoning is sound and this file does not touch it: nothing here makes `emit` raise. What it changes is
that the emitter STATES the outcome it already computed, so a caller can decide to stage. It has
already mattered twice on this lane — the trainer in 2026-07, and `service-ingest` on 2026-08-06, a day
of 403s while the data landed — and `ingest/service_identity.py` records that a 401 there *"surfaces as
a permanent gap in the graph that looks exactly like a healthy estate."*

THE ROW PRESENTED TWO SHAPES AS ALTERNATIVES AND THEY ARE COMPLEMENTARY. "Make emit report" was
costed as leaving a residual crash window, and "stage, emit, drop" as closing it — but staging cannot
know when to DROP unless emit reports. Reporting is the enabler for staging, not a lesser substitute
for it, so this lands first and the outbox wrapper builds on it.

WHAT THE BOOL MEANS is "this event needs no recovery", not "it reached the graph", and the difference
is the whole reason `NoopEmitter` answers True. A deployment with lineage switched off has not lost
anything — staging its events would grow an outbox that nothing drains, turning an opt-out into a leak.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from lineage_kit.emitter import ClientEmitter, NoopEmitter, RecordingEmitter
from lineage_kit.schemas import RunEvent, RunState


if TYPE_CHECKING:
    from openlineage.client import OpenLineageClient


def _event() -> RunEvent:
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-08T00:00:00+00:00",
            "run": {"runId": "11111111-1111-5111-8111-111111111111"},
            "job": {"namespace": "ingest", "name": "land"},
        }
    )


class _Client:
    """An OpenLineage client that fails the way a refused door does.

    Structural, then `cast` to the real type at each use — the estate's pattern for a client double
    (the Ray fake does the same). It keeps the signature honest without importing the SDK's machinery
    to construct one that must fail.
    """

    def __init__(self, *, boom: bool) -> None:
        self._boom = boom
        self.emitted: list[Any] = []

    def emit(self, payload: Any) -> None:
        if self._boom:
            raise RuntimeError("HTTP 401 — the presented credential may not claim 'service-ingest'")
        self.emitted.append(payload)


def test_a_delivered_event_reports_true() -> None:
    client = _Client(boom=False)
    assert ClientEmitter(cast("OpenLineageClient", client)).emit(_event()) is True
    assert client.emitted, "the happy path stopped emitting"


def test_a_refused_door_reports_false() -> None:
    """The headline, and the exact shape of both recorded incidents: the transport refused, the run's
    data landed anyway, and nothing downstream could tell."""
    assert ClientEmitter(cast("OpenLineageClient", _Client(boom=True))).emit(_event()) is False


def test_a_refused_door_still_does_not_raise() -> None:
    """The constraint this must not break: emission must never turn an observability outage into a
    data incident. Reporting is additive to the swallow, not a replacement for it."""
    ClientEmitter(cast("OpenLineageClient", _Client(boom=True))).emit(_event())  # no exception


def test_an_unauthorable_event_reports_false_too() -> None:
    """The OTHER failure mode, which the emitter deliberately keeps distinct: a producer that built an
    unserialisable facet never reaches the transport at all, and is just as lost."""

    class _Unauthorable(RunEvent):
        def to_openlineage(self) -> Any:
            raise ValueError("facet is not serialisable")

    event = _Unauthorable.model_validate(_event().model_dump(by_alias=True))
    client = _Client(boom=False)
    assert ClientEmitter(cast("OpenLineageClient", client)).emit(event) is False
    assert not client.emitted, "an unauthorable event reached the transport"


@pytest.mark.parametrize(
    ("emitter", "expected"),
    [(NoopEmitter(), True), (RecordingEmitter(), True)],
    ids=["noop", "recording"],
)
def test_the_emitters_that_lose_nothing_report_true(emitter: Any, expected: bool) -> None:
    """`NoopEmitter` answers True because the bool means "needs no recovery", not "reached the graph".
    Lineage switched off has lost nothing; staging its events would grow an outbox nothing drains."""
    assert emitter.emit(_event()) is expected


def test_the_recorder_still_returns_the_event_it_emitted() -> None:
    """The existing contract, pinned because E1's fix depends on it: the caller gets the event back, so
    a producer that learns the emit failed still holds the thing it needs to stage."""
    from lineage_kit.runs import LineageRun

    recording = RecordingEmitter()
    event = LineageRun(job_name="land", namespace="ingest", run_id="11111111-1111-5111-8111-111111111111", emitter=recording).start()

    assert event is not None and event.event_type is RunState.START
    assert recording.events == [event]
