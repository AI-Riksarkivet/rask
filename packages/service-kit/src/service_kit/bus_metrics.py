"""Bus-publish instruments owned by the estate, for the failures the sidecar cannot see.

Dapr already counts publishes: `dapr_component_pubsub_egress_count_total{app_id,component,success,topic}`
plus a latency histogram, and both the gRPC instrumentor and the sidecar emit a span per publish. None of
that is duplicated here — a rask-side publish counter would be a fourth copy of a fact three surfaces
already carry.

What none of them can see is a publish that is REFUSED BEFORE ANY I/O. The claim-check guard in
`dapr_publish.publish_event` raises before the gRPC call, so no span is opened, no egress row is written
and no latency sample is taken: on every free surface the event simply never existed. Of the sites that
reach that funnel, a couple happen to count a refusal themselves; the rest log and swallow or return a
retry sentinel.

NAMED `bus.*`, NOT `dapr.*`, deliberately. The latter renders `dapr_publish_refused_total`, which lands in
the middle of the sidecar's own 128-table `dapr_*` namespace — a namespace operators filter on to mean
"emitted by Dapr". A first-party instrument masquerading as one is worse than an awkward name.
"""

from __future__ import annotations

from opentelemetry import metrics


_meter = metrics.get_meter("lance.bus")

_refused = _meter.create_counter(
    "bus.publish.refused",
    unit="{event}",
    description="Publishes the claim-check guard refused BEFORE any I/O — no gRPC call happens, so the sidecar's egress counter cannot see them.",
)


def record_refusal(topic: str, reason: str) -> None:
    """Count one pre-I/O refusal.

    Both labels are bounded by construction, which is the rule this estate has already been burned by
    breaking: `topic` can only be a named settings field or constant (an inline literal is rejected by
    `test_every_publish_site_uses_a_named_topic_constant`), and `reason` is a closed set owned by this
    module. Never label with anything off the payload — the refused payload's SIZE is a log field, where
    it already is; as a label it would be one series per byte count.
    """
    _refused.add(1, {"lance.bus.topic": topic, "lance.bus.reason": reason})
