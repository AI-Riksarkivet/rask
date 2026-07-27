"""A bounded, per-replica in-memory ring buffer of control-plane change-events.

Each catalog replica subscribes to `catalog.control.v1` WITHOUT a queueGroupName (broadcast), so every
replica sees every event and appends it here. The poll endpoint (`GET /v1/events?since=<cursor>`) reads
events after a client's cursor. In-memory + bounded (drop-oldest) is correct because events are **hints**,
not the durable record (that's the audit trail): the buffer only needs to cover a poll gap, and a client
whose cursor fell off the end gets `reset=True` → the console `invalidateAll()`s and re-reads authoritative
state. Cursors are a per-replica monotonic counter; a redelivered event (same `event_id`) is deduped.

Single-process, cooperative-async access (FastAPI on one event loop) — no lock needed; append (from the Dapr
subscription handler) and `since` (from the poll handler) never interleave mid-operation.
"""

from __future__ import annotations

from collections import deque

from common.control_events import CatalogControlEvent


class ControlEventBuffer:
    """A drop-oldest ring of `(cursor, event)` with a monotonic cursor and `event_id` dedupe."""

    def __init__(self, maxsize: int) -> None:
        self._buf: deque[tuple[int, CatalogControlEvent]] = deque(maxlen=maxsize)
        self._ids: set[str] = set()
        self._cursor = 0

    @property
    def head(self) -> int:
        """The newest cursor issued (0 when nothing has been buffered yet)."""
        return self._cursor

    def append(self, event: CatalogControlEvent) -> None:
        """Store `event` under the next cursor. A redelivery (same `event_id`) is ignored. When the ring is
        full, the oldest event is dropped (and its id forgotten) before the new one lands."""
        if event.event_id in self._ids:
            return
        if self._buf.maxlen is not None and len(self._buf) == self._buf.maxlen:
            _, evicted = self._buf[0]
            self._ids.discard(evicted.event_id)
        self._cursor += 1
        self._buf.append((self._cursor, event))
        self._ids.add(event.event_id)

    def since(self, cursor: int) -> tuple[list[CatalogControlEvent], int, bool]:
        """Events strictly after `cursor`, the new head cursor, and a `reset` flag.

        A new client polls `since=0` and gets the whole retained window (≤ maxsize) — the recent activity to
        render on connect — then advances its cursor to `head` and polls incrementally. `reset=True` means a
        *positive* client cursor's next-expected event (`cursor + 1`) is older than the oldest retained event,
        i.e. it fell off the bounded buffer (overflow) and must re-read everything (`invalidateAll`).

        Note there is deliberately NO `cursor <= 0` baseline short-circuit: it would advance a fresh client's
        cursor to `head` while returning nothing, silently skipping the first events after connect (caught
        live 2026-07-23). The buffer is bounded, so returning the whole window on the first poll is safe."""
        head = self._cursor
        if not self._buf:
            return [], head, False
        oldest = self._buf[0][0]
        reset = cursor > 0 and (cursor + 1) < oldest
        events = [e for (c, e) in self._buf if c > cursor]
        return events, head, reset
