"""The one publish seam: what the sidecar's client is handed for an envelope, bounded in size and in time.

Every publish site goes through ``lakehouse.dapr_publish.publish_event``, so what holds here holds for each of them: the
envelope goes out whole as a structured CloudEvent carrying the caller's trace, one past the broker's limit is refused
before any I/O and counted, and a sidecar that does not answer raises instead of holding its caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from opentelemetry import context, trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from lakehouse import bus_metrics, dapr_publish
from lakehouse.cloud_events import CloudEvent


#: The W3C Trace Context specification's own example.
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


class _Sidecar:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        self.published.append({"pubsub_name": pubsub_name, "topic_name": topic_name, "data_content_type": data_content_type, "envelope": json.loads(data)})


def _envelope(**data: Any) -> CloudEvent:
    return CloudEvent(id="e1", source="rask/test", type="rask.test.v1", data=data)


def _publish(sidecar: object, envelope: CloudEvent, *, timeout_seconds: float = 5) -> None:
    asyncio.run(dapr_publish.publish_event(sidecar, envelope, pubsub_name="lineage-pubsub", topic_name="lineage.events.v1", timeout_seconds=timeout_seconds))


def test_an_envelope_goes_out_whole_as_a_structured_cloudevent_and_says_nothing(caplog: pytest.LogCaptureFixture) -> None:
    sidecar = _Sidecar()
    envelope = _envelope(run="r1")

    with caplog.at_level(logging.WARNING, logger=dapr_publish.__name__):
        _publish(sidecar, envelope)

    assert sidecar.published == [
        {
            "pubsub_name": "lineage-pubsub",
            "topic_name": "lineage.events.v1",
            "data_content_type": "application/cloudevents+json",
            "envelope": envelope.as_wire(),
        }
    ]
    assert caplog.records == []


@pytest.mark.parametrize(
    ("state", "carried"),
    [
        pytest.param(None, {"traceparent": TRACEPARENT, "traceid": TRACEPARENT}, id="a-span"),
        pytest.param(
            TraceState([("vendor", "1")]), {"traceparent": TRACEPARENT, "traceid": TRACEPARENT, "tracestate": "vendor=1"}, id="a-span-with-vendor-state"
        ),
    ],
)
def test_a_publish_inside_a_span_carries_its_trace_as_the_pair_the_sidecar_keeps(state: TraceState | None, carried: dict[str, str]) -> None:
    """``traceid`` beside an equal ``traceparent``: the sidecar blanks a ``traceparent`` published without its twin, and
    the subscriber's trace is then all zeros (measured on daprd 1.19.0-rc.4, 2026-11-13)."""
    sidecar = _Sidecar()
    envelope = _envelope(run="r1")
    span = SpanContext(
        trace_id=0x0AF7651916CD43DD8448EB211C80319C, span_id=0xB7AD6B7169203331, is_remote=False, trace_flags=TraceFlags(TraceFlags.SAMPLED), trace_state=state
    )
    token = context.attach(trace.set_span_in_context(NonRecordingSpan(span)))

    try:
        _publish(sidecar, envelope)
    finally:
        context.detach(token)

    assert [published["envelope"] for published in sidecar.published] == [envelope.as_wire() | carried]


def test_an_envelope_past_the_brokers_limit_is_refused_before_any_io_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """No call reaches the sidecar, so no span and no egress row exists downstream: this counter is the refusal's only trace."""
    recorded: list[tuple[int, dict[str, str]]] = []
    monkeypatch.setattr(bus_metrics, "_refused", type("C", (), {"add": lambda _s, n, a=None: recorded.append((n, dict(a or {})))})())
    sidecar = _Sidecar()

    with pytest.raises(ValueError, match="POINTERS"):
        _publish(sidecar, _envelope(blob="x" * (dapr_publish.MAX_PAYLOAD_BYTES + 1)))

    assert (sidecar.published, recorded) == ([], [(1, {"lance.bus.topic": "lineage.events.v1", "lance.bus.reason": "oversize"})])


def test_a_large_envelope_is_published_and_warned_about(caplog: pytest.LogCaptureFixture) -> None:
    sidecar = _Sidecar()
    envelope = _envelope(blob="x" * (dapr_publish.WARN_PAYLOAD_BYTES + 1))

    with caplog.at_level(logging.WARNING, logger=dapr_publish.__name__):
        _publish(sidecar, envelope)

    assert ([published["envelope"] for published in sidecar.published], [record.message for record in caplog.records]) == (
        [envelope.as_wire()],
        ["dapr_publish_payload_large"],
    )


def test_a_sidecar_that_does_not_answer_raises_a_timeout() -> None:
    class _Hung:
        async def publish_event(self, **_kwargs: Any) -> None:
            await asyncio.sleep(60)

    with pytest.raises(TimeoutError):
        _publish(_Hung(), _envelope(run="r1"), timeout_seconds=0.05)
