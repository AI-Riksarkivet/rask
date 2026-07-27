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
import logging
from typing import Any


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
