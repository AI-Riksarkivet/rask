"""The bus instruments the lakehouse owns: how long each publish took and how it ended, and each publish refused before
anything was sent.

A PUBLISH IS MEASURED WHERE IT IS MADE (`lakehouse.bus.publish_event`). The process publishes on its own connection, so
nothing else counts it. One histogram carries what an operator reads for a boundary: its count is the traffic, its
`error.type` attribute the failures, its buckets the latency. It is named and labelled by OpenTelemetry's messaging
conventions (`messaging.client.operation.duration`), so a backend's stock messaging views read it.

A PUBLISH REFUSED BEFORE ANYTHING IS SENT is counted on its own as well (`bus.publish.refused`), by the rule that
refused it: the claim-check guard is a contract an alert names, where the histogram would show only a `ValueError`.

NAMED `bus.*`: the instruments say what the lakehouse did, whichever client and broker carry it.
"""

from __future__ import annotations

from collections.abc import Mapping

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
