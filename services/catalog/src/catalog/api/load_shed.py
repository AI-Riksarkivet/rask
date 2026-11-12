"""Load-shedding — a pure-ASGI middleware that caps CONCURRENT in-flight bulk table writes.

Each ``/v1/table/{id}/{create,insert,merge_insert}`` buffers the whole request body in memory before the
handler runs, whatever its Content-Type names (up to ``max_body_bytes`` = 256MiB, see body_limit.py), so N concurrent writes = N × 256MiB —
the OOM that the per-workload memory tier (catalog=1Gi in values-prod) only PARTLY bounds. Under a write
burst the pod OOMs and takes every in-flight request with it. This sheds the overflow: once
``max_concurrent`` writes are reading their bodies, the next is rejected with **429** (the ``THROTTLING`` → 429
mapping in lakehouse/ns_errors.py that had no producer) + a ``Retry-After``, BEFORE a single body byte is
buffered — so shedding actually relieves
memory pressure rather than adding to it. Reads and non-bulk writes (``version/create``, ``update``, ``delete``)
pass through untouched. ``max_concurrent=0`` disables it entirely.

A WRITE TAKES ITS SEAT WHEN ITS BODY IS FIRST READ, not when it arrives. The catalog judges a caller from its headers
before its body is read (`lakehouse.admission`), so a request it refuses never reads its body and never sits. Seated on
arrival, an upload with no token holds one of the two production seats for as long as it trickles in, and every
authenticated write behind it is shed with 429.

A bulk write is known by its ROUTE, never by its header: the handlers take the raw body whatever the Content-Type says,
so a write that names another media type, or none, buffers as much. pylance's stock REST client sends an insert with no
Content-Type at all (measured on pylance 13.0.0, `RestNamespace.insert_into_table`). The path it reads is the one the outermost
``PathReadingMiddleware`` leaves, the public prefix stripped and an escaped ``/`` already refused.

Pure ASGI (not ``BaseHTTPMiddleware``) so the reject happens before the body is read, mirroring body_limit.
The in-flight counter is a plain int: the event loop is single-threaded and there is NO await between the
capacity check and the increment, so the read-modify-write is atomic (no lock needed).
"""

from __future__ import annotations

import re

from lance_namespace import ErrorCode
from opentelemetry import metrics
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lakehouse.problem import send_problem
from lakehouse.read_refusal import ReadRefusal


#: The three bulk table writes, ``POST /v1/table/<one id segment>/{create,insert,merge_insert}``, the routes that buffer a
#: large body. An id is one segment (a nested id joins its parts with a delimiter), so the cheap JSON doors that also end in
#: ``/create`` (``/v1/namespace/{id}/create``, ``/v1/table/{id}/tags/create``, ``/v1/table/{id}/version/create``,
#: ``/v1/materialized_view/{id}/create``) are not matched, nor are ``update`` and ``delete``, which are predicate-driven.
_BULK_WRITE = re.compile(r"/v1/table/[^/]+/(?:create|insert|merge_insert)")


def _is_bulk_write(scope: Scope) -> bool:
    """A POST to a bulk table write's route, the large buffer to shed, whatever its Content-Type."""
    return scope.get("method") == "POST" and _BULK_WRITE.fullmatch(scope.get("path", "")) is not None


_meter = metrics.get_meter("lance.catalog")
_writes_shed = _meter.create_counter(
    "catalog.writes.shed",
    unit="{request}",
    description="Bulk table write requests SHED with 429 because the concurrent-write cap was reached (P5).",
)


class WriteConcurrencyLimitMiddleware:
    """Reject a bulk table write with 429 once ``max_concurrent`` are already reading their bodies."""

    def __init__(self, app: ASGIApp, *, max_concurrent: int) -> None:
        self._app = app
        self._max = max_concurrent
        self._inflight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._max <= 0 or scope["type"] != "http" or not _is_bulk_write(scope):
            await self._app(scope, receive, send)
            return
        # Shed in the read, as the body cap refuses, so the 429 survives whoever reads the body (`lakehouse.read_refusal`).
        refusal = ReadRefusal(send, self._shed)
        seated = False

        async def seated_receive() -> Message:
            nonlocal seated
            if not seated:
                if self._inflight >= self._max:  # at capacity → shed BEFORE a body byte is read
                    await refusal.refuse()
                self._inflight += 1  # no await between the check above and here → atomic on the single-threaded loop
                seated = True
            return await receive()

        try:
            await refusal.run(self._app, scope, seated_receive)
        finally:
            if seated:
                self._inflight -= 1

    async def _shed(self, send: Send) -> None:
        # THROTTLING is the spec code this refusal means, and `Retry-After` tells a client when to try again.
        _writes_shed.add(1)
        await send_problem(
            send,
            ErrorCode.THROTTLING,
            status=429,
            title="Too Many Requests",
            slug="throttling",
            detail=(
                "the catalog is at its concurrent-write capacity; retry with backoff, or write large "
                "payloads directly to object storage with vended credentials"
            ),
            headers=[(b"retry-after", b"1")],
        )
