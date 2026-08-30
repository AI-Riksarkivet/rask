"""Publish to the Dapr sidecar under a TIGHT per-site timeout — a hung sidecar must not pin the worker.

The Dapr async SDK already bounds every unary RPC (incl. ``PublishEvent``) with a client-side gRPC
deadline: ``DaprClientTimeoutInterceptorAsync`` applies ``DAPR_API_TIMEOUT_SECONDS`` on every call, and our
chart sets that to 30s on every app pod as the global backstop. But that knob is a blunt GLOBAL deadline for
every SDK gRPC call, and ``publish_event`` exposes no per-call timeout arg (the secret-store fetch is plain
HTTP with its own 5s-per-attempt bound — unaffected either way). So we wrap each publish in a deliberately
tighter ``asyncio.timeout`` (default 5s) that fires
well before the 30s gRPC deadline, turning a wedged sidecar / NATS stall into a ``TimeoutError`` the existing
failure path already handles (mover → RETRY / redeliver; catalog+compaction emit → best-effort swallow)
without a coroutine sitting stalled for 30s. One helper so the tighter bound is applied at every publish site.

CLAIM-CHECK GUARD (§9 P1, 2026-07-11): events carry POINTERS (dataset/version/URI), never data — NATS
JetStream's default max message is ~1 MiB, so a data-shaped payload would fail at the broker ANYWAY,
just later and with an opaque transport error after the timeout. Because every publish site funnels
through this ONE helper, the invariant is enforced here: a payload past the hard cap raises
``ValueError`` naming the rule (fail fast, same failure semantics the caller already handles — the
publish did not happen either way), and a payload past the soft cap logs a WARNING (early visibility
for facet-bloat creep, docs/DATA-CONTRACT.md §4) without changing behavior.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

from service_kit.bus_metrics import record_refusal


log = logging.getLogger(__name__)

#: Soft cap — a legitimate event (facets incl. a wide schema) stays well under this; crossing it is
#: the facet-bloat early warning (§9 P2 tracks a real truncation cap), not an error.
WARN_PAYLOAD_BYTES = 64 * 1024
#: Hard cap — just under NATS JetStream's ~1 MiB default max_payload. The broker would reject the
#: publish anyway; failing HERE turns an opaque late transport error into a claim-check violation
#: with a clear name. Behavior-preserving by construction: no payload under the broker limit is
#: refused, and nothing over it could ever have been delivered.
MAX_PAYLOAD_BYTES = 900 * 1024


def _payload_bytes(data: Any) -> int:
    if isinstance(data, bytes):
        return len(data)
    if isinstance(data, str):
        return len(data.encode())
    return 0  # non-str/bytes payloads are the SDK's concern; the guard covers our JSON strings


async def publish_event(publisher: Any, *, timeout_seconds: float, **kwargs: Any) -> None:
    """``await publisher.publish_event(**kwargs)`` bounded by ``timeout_seconds`` (raises ``TimeoutError``).

    ``publisher`` is any Dapr client (``dapr.aio.clients.DaprClient`` / the lineage emitter's client) — typed
    ``Any`` because their concrete ``publish_event`` signatures differ; the helper only forwards + times out.
    Raises ``ValueError`` (before any I/O) for a payload past ``MAX_PAYLOAD_BYTES`` — the claim-check
    invariant: events carry pointers, never data.
    """
    size = _payload_bytes(kwargs.get("data"))
    if size > MAX_PAYLOAD_BYTES:
        # Counted because this aborts BEFORE the gRPC call: no client span, no sidecar span, and no
        # `dapr_component_pubsub_egress_count_total` row. Every free surface that can see a publish
        # failure sits downstream of a call this branch never makes.
        record_refusal(str(kwargs.get("topic_name", "?")), "oversize")
        raise ValueError(
            f"event payload is {size} bytes (> {MAX_PAYLOAD_BYTES}): events carry POINTERS, never "
            f"data (claim-check invariant, docs/DATA-CONTRACT.md) — topic "
            f"{kwargs.get('topic_name', '?')}"
        )
    if size > WARN_PAYLOAD_BYTES:
        log.warning(
            "dapr_publish_payload_large",
            extra={"bytes": size, "topic": kwargs.get("topic_name", "?")},
        )
    async with asyncio.timeout(timeout_seconds):
        await publisher.publish_event(**kwargs)


async def publish_json(
    publisher: Any,
    *,
    pubsub_name: str,
    topic_name: str,
    payload: Any,
    timeout_seconds: float,
    failure_event: str,
    context: Mapping[str, Any] | None = None,
) -> bool:
    """Publish a JSON payload and REPORT rather than raise. ``True`` when it landed.

    THE PUBLISH-AND-REPORT SHAPE, ONCE (open_python-audit DUP-18). Five medallion call sites each
    serialized their payload, set ``application/json``, wrapped the call in their own ``try`` and
    logged their own warning — and the five reports had drifted into four different field sets. Two
    omitted the topic; one omitted the token, which is the only thing that joins a failed trigger to
    the cascade it belongs to; one omitted the ``error`` string the other four lead with. What a
    publish failure carries decides whether an operator can find the run, so it is decided here.

    ``failure_event`` stays per-site: the log's own name is how a reader knows WHICH publish broke,
    and one shared event name would erase that. ``context`` is the site's identifying fields (token,
    dataset, stage); ``topic`` and ``error`` are added here so no site can forget them, and the
    traceback rides along because a broker failure's cause is in it.

    Returns rather than raises because every one of those sites has already decided what a failed
    publish MEANS for it — RETRY the delivery, degrade to a permanent BLOCK, report ``publish_failed``
    to the caller — and none of them can act on an exception. The bound, the claim-check guard and the
    oversize refusal are ``publish_event``'s, above; this only adds serialization and the report.
    """
    try:
        await publish_event(
            publisher,
            timeout_seconds=timeout_seconds,
            pubsub_name=pubsub_name,
            topic_name=topic_name,
            data=json.dumps(payload),
            data_content_type="application/json",
        )
    except Exception as exc:
        log.warning(failure_event, extra={**(context or {}), "topic": topic_name, "error": str(exc)}, exc_info=True)
        return False
    return True
