"""Publish one enveloped event through the Dapr sidecar: whole, sized, traced, and under a tight timeout.

THE ENVELOPE IS THE BODY (`lakehouse.cloud_events`), in structured mode under ``application/cloudevents+json``. The
sidecar passes such a body through as the CloudEvent it is and takes its ``id`` as the message's ``Nats-Msg-Id``
(measured on daprd 1.19.0-rc.4 against nats-server 2.15.2, 2026-11-13: ``id``, ``type``, ``source``, ``subject`` and
``data`` are stored as sent, and a second publish of one ``id`` inside the stream's duplicate window is stored once). A
body published as ``application/json`` is wrapped under an id the sidecar mints per publish, which no relay's copy of
the event shares.

A TIGHT TIMEOUT PER SITE, so a sidecar that does not answer cannot hold its caller. The Dapr SDK bounds every unary RPC
with ``DAPR_API_TIMEOUT_SECONDS``, which the chart sets to 30 s on every app pod, and its ``publish_event`` takes no
bound of its own. Each publish is wrapped in ``asyncio.timeout`` instead, so a wedged sidecar or a stalled bus is a
``TimeoutError`` the caller's failure path already handles: a delivered route answers RETRY, an emitter counts it and
leaves its event staged.

THE CLAIM-CHECK GUARD. Events carry pointers (a dataset, a version, a URI), never data, and JetStream's default maximum
message is about 1 MiB, so a data-shaped payload would fail at the broker anyway, later and as an opaque transport
error. Every publish site goes through this one function, so the rule is enforced here: an envelope past the hard cap
raises ``ValueError`` naming the rule before any I/O, and one past the soft cap is published and logged, the early
sight of facet bloat (docs/DATA-CONTRACT.md §4).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Final

from opentelemetry import propagate

from lakehouse.bus_metrics import record_refusal
from lakehouse.cloud_events import STRUCTURED_CONTENT_TYPE, CloudEvent


log = logging.getLogger(__name__)

#: Soft cap: an event with a wide schema facet stays well under it, so crossing it is worth a line, not a refusal.
WARN_PAYLOAD_BYTES = 64 * 1024
#: Hard cap, just under JetStream's default ``max_payload`` of about 1 MiB. No envelope under the broker's limit is
#: refused, and none over it could have been delivered.
MAX_PAYLOAD_BYTES = 900 * 1024

#: The W3C Trace Context fields CloudEvents' distributed tracing extension carries as attributes of the same names.
_TRACE_FIELDS: Final = ("traceparent", "tracestate")


def _trace_attributes() -> dict[str, str]:
    """The caller's trace context as envelope attributes, with the twin the sidecar needs beside it.

    ``traceid`` EQUAL TO ``traceparent`` IS WHAT MAKES THE SIDECAR KEEP BOTH. Measured on daprd 1.19.0-rc.4, 2026-11-13:
    an envelope carrying ``traceparent`` and no ``traceid`` is stored with both blank and its subscriber is handed an
    all-zero ``traceparent``; with the pair both survive, and the subscriber's request continues the publisher's trace.
    Outside any span there is nothing to carry, and the sidecar stamps its own.
    """
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    carried = {field: carrier[field] for field in _TRACE_FIELDS if field in carrier}
    if "traceparent" not in carried:
        return {}
    return carried | {"traceid": carried["traceparent"]}


async def publish_event(publisher: Any, envelope: CloudEvent, *, pubsub_name: str, topic_name: str, timeout_seconds: float) -> None:
    """Publish ``envelope`` on ``topic_name`` through the sidecar's client, within ``timeout_seconds``.

    Args:
        publisher: A Dapr client. ``Any`` because the concrete clients' ``publish_event`` signatures differ.
        envelope: The event in its envelope; it goes out with the caller's trace context added.
        pubsub_name: The pub/sub component to publish through.
        topic_name: The topic. A settings field or a constant, never a value off a payload: it labels the refusal counter.
        timeout_seconds: How long the sidecar has to take the publish.

    Raises:
        ValueError: The envelope is past ``MAX_PAYLOAD_BYTES``. Nothing was sent, and the refusal is counted.
        TimeoutError: The sidecar did not take the publish in time.
    """
    body = json.dumps(envelope.as_wire() | _trace_attributes())
    size = len(body.encode())
    if size > MAX_PAYLOAD_BYTES:
        # Counted because this ends before the gRPC call: no client span, no sidecar span and no egress row exist for
        # it, and every surface that can see a failed publish sits downstream of a call this branch never makes.
        record_refusal(topic_name, "oversize")
        raise ValueError(
            f"event payload is {size} bytes (> {MAX_PAYLOAD_BYTES}): events carry POINTERS, never data "
            f"(claim-check invariant, docs/DATA-CONTRACT.md) — topic {topic_name}"
        )
    if size > WARN_PAYLOAD_BYTES:
        log.warning("dapr_publish_payload_large", extra={"bytes": size, "topic": topic_name})
    async with asyncio.timeout(timeout_seconds):
        await publisher.publish_event(pubsub_name=pubsub_name, topic_name=topic_name, data=body, data_content_type=STRUCTURED_CONTENT_TYPE)
