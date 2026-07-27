"""Load-shedding — a pure-ASGI middleware that caps CONCURRENT in-flight Arrow-IPC writes.

Each ``/v1/table/{id}/{create,insert,merge_insert}`` buffers the whole request body in memory before the
handler runs (up to ``max_body_bytes`` = 256MiB, see body_limit.py), so N concurrent writes = N × 256MiB —
the OOM that the per-workload memory tier (catalog=1Gi in values-prod) only PARTLY bounds. Under a write
burst the pod OOMs and takes every in-flight request with it. This sheds the overflow: once
``max_concurrent`` writes are in flight, the next is rejected with **429** (the ``THROTTLING`` → 429
mapping in service_kit/lakehouse/ns_errors.py that had no producer) + a ``Retry-After``, BEFORE a single body byte is
buffered — so shedding actually relieves
memory pressure rather than adding to it. Reads and non-bulk writes (``/commit``, ``update``, ``delete``)
pass through untouched. ``max_concurrent=0`` disables it entirely.

Pure ASGI (not ``BaseHTTPMiddleware``) so the reject happens before the body is read, mirroring body_limit.
The in-flight counter is a plain int: the event loop is single-threaded and there is NO await between the
capacity check and the increment, so the read-modify-write is atomic (no lock needed).
"""

from __future__ import annotations

import json

from opentelemetry import metrics
from starlette.types import ASGIApp, Receive, Scope, Send


_PROBLEM_JSON = b"application/problem+json"
# Only the bulk Arrow-IPC body writes — the ones that buffer a large body. /commit (a small metadata op) and
# update/delete (predicate-driven) are deliberately NOT gated: they don't carry the multi-hundred-MiB payload.
_WRITE_SUFFIXES = ("/create", "/insert", "/merge_insert")
# The discriminator is the Arrow-IPC content-type, NOT the path suffix alone: `/create` also ends the CHEAP
# metadata endpoints (`/v1/namespace/{id}/create`, `/v1/table/{id}/tags/create`, `.../version/create`,
# `/v1/materialized_view/{id}/create`) which send JSON and buffer nothing — shedding those under write
# pressure would be nonsense. The bulk table writes are exactly the ones that POST an Arrow stream.
_ARROW_IPC = b"application/vnd.apache.arrow.stream"


def _is_bulk_arrow_write(scope: Scope) -> bool:
    """A POST to a write endpoint that actually carries an Arrow-IPC body (the large buffer to shed)."""
    if scope.get("method") != "POST":
        return False
    if not any(scope.get("path", "").endswith(s) for s in _WRITE_SUFFIXES):
        return False
    for name, value in scope.get("headers", ()):
        if name == b"content-type":
            return value.startswith(_ARROW_IPC)
    return False


_meter = metrics.get_meter("lance.catalog")
_writes_shed = _meter.create_counter(
    "catalog.writes.shed",
    unit="{request}",
    description="Arrow-IPC write requests SHED with 429 because the concurrent-write cap was reached (P5).",
)


class WriteConcurrencyLimitMiddleware:
    """Reject a bulk Arrow-IPC write with 429 once ``max_concurrent`` are already in flight."""

    def __init__(self, app: ASGIApp, *, max_concurrent: int) -> None:
        self._app = app
        self._max = max_concurrent
        self._inflight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._max <= 0 or scope["type"] != "http" or not _is_bulk_arrow_write(scope):
            await self._app(scope, receive, send)
            return

        if self._inflight >= self._max:  # at capacity → shed BEFORE buffering the body
            _writes_shed.add(1)
            await self._reject(send)
            return

        self._inflight += 1  # no await between the check above and here → atomic on the single-threaded loop
        try:
            await self._app(scope, receive, send)
        finally:
            self._inflight -= 1

    async def _reject(self, send: Send) -> None:
        body = json.dumps(
            {
                "type": "https://lance.org/problems/throttling",
                "title": "Too Many Requests",
                "status": 429,
                "detail": (
                    "the catalog is at its concurrent-write capacity; retry with backoff, or write large "
                    "payloads directly to object storage with vended credentials"
                ),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", _PROBLEM_JSON),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", b"1"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
