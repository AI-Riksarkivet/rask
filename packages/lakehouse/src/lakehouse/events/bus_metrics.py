"""The bus instruments the lakehouse owns: how long each publish took and how it ended, each publish refused before
anything was sent, and how each delivery a process took was settled and how long its handling took.

A PUBLISH IS MEASURED WHERE IT IS MADE (`lakehouse.events.bus.publish_event`). The process publishes on its own connection, so
nothing else counts it. One histogram carries what an operator reads for a boundary: its count is the traffic, its
`error.type` attribute the failures, its buckets the latency. It is named and labelled by OpenTelemetry's messaging
conventions (`messaging.client.operation.duration`), so a backend's stock messaging views read it.

A PUBLISH REFUSED BEFORE ANYTHING IS SENT is counted on its own as well (`bus.publish.refused`), by the rule that
refused it: the claim-check guard is a contract an alert names, where the histogram would show only a `ValueError`.

A DELIVERY IS COUNTED BY HOW IT WAS SETTLED (`bus.deliveries`), under the consumer it was taken from: taken, asked for
again, parked, or, on a followed subject, failed. Each series is created at 0 when its consumer starts
(:func:`create_delivery_series`), since an alert over a counter reads its increase, and a series whose first sample is
1 shows none. How long each handling took is its own histogram, under the messaging conventions' name for it
(`messaging.process.duration`).

AN ASK THE BUS REFUSED IS COUNTED (`bus.pulls.failed`), under the consumer asked for. A lane binds to a consumer the
chart's stream Job declares and creates none, so a consumer or a stream the Job did not converge shows only here: the
lane asks again without end and takes nothing.

NAMED `bus.*`: the instruments say what the lakehouse did, whichever client and broker carry it.
"""

from collections.abc import Iterable, Mapping

from opentelemetry import metrics


_meter = metrics.get_meter("lance.bus")

_refused = _meter.create_counter(
    "bus.publish.refused",
    unit="{event}",
    description="Publishes the claim-check guard refused before anything was sent to the bus.",
)

_duration = _meter.create_histogram(
    "messaging.client.operation.duration",
    unit="s",
    description=(
        "How long each publish to the bus took, from the call to the stream's acknowledgement or the failure; "
        "`error.type` is set on a publish that was not delivered."
    ),
    # The boundaries OpenTelemetry's messaging conventions advise for this instrument, in seconds. Without them the
    # SDK's defaults apply, which are spaced for milliseconds (0, 5, 10, 25, ...), and every publish lands in the first.
    explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10],
)


_deliveries = _meter.create_counter(
    "bus.deliveries",
    unit="{message}",
    description="Deliveries this process took from the bus, by the consumer they came from and how each was settled.",
)

_pulls_failed = _meter.create_counter(
    "bus.pulls.failed",
    unit="{pull}",
    description="Asks for a consumer's next message, or a followed subject's, that the bus refused or dropped, by consumer.",
)

_processing = _meter.create_histogram(
    "messaging.process.duration",
    unit="s",
    description="How long the handling of each delivery took, from the message's arrival to its settlement.",
    explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10],
)


def create_delivery_series(consumer: str, outcomes: Iterable[str]) -> None:
    """Create ``consumer``'s series of `bus.deliveries` at 0, one per outcome it can settle a delivery with, and its
    series of `bus.pulls.failed`."""
    for outcome in outcomes:
        _deliveries.add(0, {"lance.bus.consumer": consumer, "lance.bus.outcome": outcome})
    _pulls_failed.add(0, {"lance.bus.consumer": consumer})


def record_pull_failed(consumer: str) -> None:
    """Count one ask the bus refused or dropped: a consumer or stream it does not hold as the chart declares it, a
    permission it does not grant, or a bus that did not answer. ``consumer`` as :func:`record_delivery` takes it."""
    _pulls_failed.add(1, {"lance.bus.consumer": consumer})


def record_delivery(consumer: str, outcome: str) -> None:
    """Count one delivery settled.

    Args:
        consumer: The durable consumer it was taken from, or the subject a process follows: a chart value or a
            constant, never a value off a message.
        outcome: How it was settled, a closed set: `lakehouse.events.bus_delivery.Outcome`, or ``failed`` for a followed
            message whose handling raised.
    """
    _deliveries.add(1, {"lance.bus.consumer": consumer, "lance.bus.outcome": outcome})


def record_processing(attributes: Mapping[str, str], seconds: float) -> None:
    """Record how long one delivery's handling took, under the messaging attributes of its consumer."""
    _processing.record(seconds, attributes)


def record_refusal(topic: str, reason: str) -> None:
    """Count one pre-I/O refusal.

    Both labels are bounded by construction, which is the rule this estate has already been burned by
    breaking: `topic` must be a named settings field or constant, never an inline literal, and
    `reason` is a closed set owned by this module. Never label with anything off the payload — the refused payload's SIZE is a log field, where
    it already is; as a label it would be one series per byte count.
    """
    _refused.add(1, {"lance.bus.topic": topic, "lance.bus.reason": reason})


def record_publish(attributes: Mapping[str, str], seconds: float, *, failure: str | None) -> None:
    """Record one publish: how long it took and, when it was not delivered, the type of what it raised.

    Args:
        attributes: The messaging attributes of the publish, its topic among them. Bounded, like `record_refusal`'s.
        seconds: From the call to the acknowledgement or the failure.
        failure: The exception's class name, a closed set by construction; None for a delivered publish.
    """
    _duration.record(seconds, {**attributes, "error.type": failure} if failure is not None else attributes)


__all__ = [
    "create_delivery_series",
    "record_delivery",
    "record_processing",
    "record_publish",
    "record_pull_failed",
    "record_refusal",
]
