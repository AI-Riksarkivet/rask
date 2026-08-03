"""The unit worker — fetch, validate, write a fragment, ack.

One worker is one competing consumer on the run's queue-group subscription. Scaling is adding pods;
there is no partitioning to get wrong, because JetStream hands each unit to exactly one consumer.

**The ack discipline is the whole design.** A unit is acked only after its fragment is on disk. Ack
early and a crash loses work the stream no longer holds; ack late — or hold an ack across a long
fetch without heartbeating — and `ack_wait` expires so JetStream redelivers work that is already in
flight, doubling requests against the rate-limited source we are trying to be gentle with.

**Validation happens BEFORE the byte is accepted**, using `packages/validate` — which has had zero
consumers since it was written. A corrupt TIFF becomes a tracked error and a DLQ entry, not a
poisoned dataset that fails months later at read time.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol

import pyarrow as pa
from pydantic import BaseModel, Field

from ingest.lander import write_unit_fragments
from ingest.queue import UnitTask, WorkQueue


if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# How many units one fetch() pulls. One round trip per batch instead of per unit — across millions of
# units that difference is the run's wall clock, not a micro-optimisation.
FETCH_BATCH = 16

# Bounded parallel fetching within a batch. The expensive resource is the SOURCE, not us: IIIF and
# HCP are rate-limited external systems, so this is a politeness ceiling as much as a throughput one.
FETCH_CONCURRENCY = 8


class ChunkOutcome(BaseModel):
    """What a drained chunk reports: fragments to commit, and what refused to land."""

    chunk_id: str
    fragments: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
    units_done: int = 0


class Fetcher(Protocol):
    """Fetches one unit's bytes. A Protocol so a worker is testable without the network."""

    async def fetch(self, key: str) -> bytes: ...


class Validator(Protocol):
    """Returns None if the bytes are acceptable, or a reason string if not."""

    def check(self, key: str, payload: bytes) -> str | None: ...


class AcceptAll:
    def check(self, key: str, payload: bytes) -> str | None:
        return None if payload else "empty payload"


def units_to_table(units: Sequence[tuple[str, bytes]]) -> pa.Table:
    """Build the bronze batch: the data AS RECEIVED plus the acquisition facts.

    Bronze is faithful to source (§3.5) — no decoding, no conversion. `id` is a stable hash of the
    source URI so a re-run converges on merge rather than duplicating, and it exists before the run
    does, which is why this plane needs no id minted mid-saga.
    """
    import hashlib

    ids = [int.from_bytes(hashlib.sha256(k.encode()).digest()[:8], "big", signed=True) for k, _ in units]
    return pa.table(
        {
            "id": pa.array(ids, pa.int64()),
            "source_uri": pa.array([k for k, _ in units], pa.string()),
            "payload": pa.array([p for _, p in units], pa.binary()),
        },
        schema=pa.schema(
            [
                pa.field("id", pa.int64()),
                pa.field("source_uri", pa.string()),
                pa.field("payload", pa.binary()),
            ]
        ),
    )


class Worker:
    """Consumes one run's units until the chunk drains."""

    def __init__(self, queue: WorkQueue, fetcher: Fetcher, validator: Validator | None = None, name: str = "w0") -> None:
        self._q = queue
        self._fetch = fetcher
        self._validate = validator or AcceptAll()
        self._name = name

    async def _one(self, task: UnitTask) -> tuple[str, bytes] | tuple[None, str]:
        payload = await self._fetch.fetch(task.key)
        reason = self._validate.check(task.key, payload)
        if reason is not None:
            return None, reason
        return task.key, payload

    async def drain_chunk(self, run_id: str, chunk_id: str, expected: int, dataset_uri: str) -> ChunkOutcome:
        """Pull units until `expected` are accounted for, then signal the waiting workflow.

        Counts errors toward completion deliberately: a chunk whose units all failed must still
        drain, or the workflow waits on a signal that can never come and the run hangs instead of
        completing-with-errors.
        """
        sub = await self._q.subscribe(run_id)
        outcome = ChunkOutcome(chunk_id=chunk_id)
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        while outcome.units_done + len(outcome.errors) < expected:
            try:
                msgs = await sub.fetch(min(FETCH_BATCH, expected), timeout=30)
            except TimeoutError:
                # No units available right now. Not an error — another worker may hold them.
                break

            async def handle(msg: object) -> None:
                task = UnitTask.model_validate_json(msg.data)  # type: ignore[attr-defined]
                async with sem:
                    try:
                        key, result = await self._one(task)
                    except Exception as exc:
                        await msg.nak()  # type: ignore[attr-defined]
                        logger.warning("unit %s failed, redelivering: %s", task.key, exc)
                        return
                    if key is None:
                        # Validation refused it: park and ACK. Redelivering corrupt bytes cannot help.
                        outcome.errors[task.key] = str(result)
                        await self._q.park_poison(task, str(result))
                        await msg.ack()  # type: ignore[attr-defined]
                        return
                    # Fragment on disk BEFORE the ack — the stream is the ledger, so an ack is a
                    # promise the work survived.
                    outcome.fragments.extend(write_unit_fragments(dataset_uri, units_to_table([(key, result)])))
                    outcome.units_done += 1
                    await msg.ack()  # type: ignore[attr-defined]

            await asyncio.gather(*(handle(m) for m in msgs))

        await self._q.signal_drained(run_id, chunk_id, outcome.model_dump())
        return outcome
