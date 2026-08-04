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
import os
from typing import TYPE_CHECKING, Any, Protocol

import pyarrow as pa
from pydantic import BaseModel, Field

from ingest.lander import write_unit_fragments
from ingest.queue import UnitTask, WorkQueue
from ingest.staging import stage_fragments


if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# THREE INDEPENDENT AXES, and conflating them is what produced a fragment per image.
#
# They answer to three different systems and there is no reason for them to agree:
#
#   workflow fan-out   `workflow.CHUNK_SIZE`  — keys per CHILD WORKFLOW. A DAPR concern: a million
#                                               activity results would melt the state store. Nothing
#                                               to do with Lance.
#   source politeness  FETCH_* below          — in-flight requests against the SOURCE. A IIIF/HCP
#                                               concern: rate limits, not throughput.
#   storage layout     FRAGMENT_TARGET_*      — rows per LANCE FRAGMENT. A Lance concern.
#
# The third one did not exist. Every unit was written as its own fragment
# (`units_to_table([(key, result)])` — a list of ONE), so a 10k-page volume produced 10k fragments in
# a single commit, 10k staging manifests, and 10k FragmentMetadata blobs across a Dapr boundary.
# Lance's own guidance is ~1M rows per fragment, and its ingestion notes name the per-row write as
# the anti-pattern: "each call commits a new version and a new fragment".
#
# All three are env-overridable, because the right value is a property of the deployment (source
# rate limit, page size, object-store latency) and not of this file.

# How many units one fetch() pulls from the queue. One round trip per batch instead of per unit.
FETCH_BATCH = int(os.getenv("RASK_INGEST_FETCH_BATCH", "16"))

# Bounded parallel fetching. The expensive resource is the SOURCE, not us: IIIF and HCP are
# rate-limited external systems, so this is a politeness ceiling as much as a throughput one.
FETCH_CONCURRENCY = int(os.getenv("RASK_INGEST_FETCH_CONCURRENCY", "8"))

# Rows accumulated before ONE fragment is written. Capped well under the queue's `max_ack_pending`
# because the batch is held UNACKED while it fills — see the ack contract in `drain_chunk`.
FRAGMENT_TARGET_ROWS = int(os.getenv("RASK_INGEST_FRAGMENT_ROWS", "1024"))

# …or this many payload bytes, whichever comes first. Page images are megabytes: a row-only trigger
# would let one fragment reach tens of gigabytes on a large-format volume, and Lance's sizing note
# puts the sane upper range at 10-100 GB per fragment with 1 TB a hard ceiling.
FRAGMENT_TARGET_BYTES = int(os.getenv("RASK_INGEST_FRAGMENT_BYTES", str(256 * 1024 * 1024)))


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

    `payload` is a BLOB column (`blob_array` against `blob_field`), not `pa.binary()`. The plain
    binary version put every page image INLINE in the `.lance` data file — no dedicated tier (a
    multi-MB page belongs in its own `.blob`, REFERENCED rather than re-copied by every compaction),
    no packed tier (nothing protecting against small-file explosion), and no `read_blobs` /
    `take_blobs` / `read_blob_ranges` for readers, so the viewer could only ever load whole rows.
    The code this plane replaced already got this right (`medallion/services/ingest.py:31`).
    """
    import hashlib

    from lance import blob_array

    from ingest.runtime import BRONZE_SCHEMA

    ids = [int.from_bytes(hashlib.sha256(k.encode()).digest()[:8], "big", signed=True) for k, _ in units]
    return pa.table(
        {
            "id": pa.array(ids, pa.int64()),
            "source_uri": pa.array([k for k, _ in units], pa.string()),
            "payload": blob_array([p for _, p in units]),
        },
        schema=BRONZE_SCHEMA,
    )


#: HTTP statuses that no retry can improve. 429 is deliberately ABSENT — it means "slow down", which
#: is the one 4xx that says try again. 408 and 425 likewise ask for a retry rather than forbidding one.
PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 405, 410, 414, 451})


def _is_permanent(exc: BaseException) -> bool:
    """Whether a fetch failure will still be a failure on the next attempt.

    Reads the status off an httpx `HTTPStatusError` when there is one; `ValueError` covers the
    fetcher's own refusals (an unresolvable scheme, a malformed key) and a `FileNotFoundError` is a
    local path that does not exist. Anything unrecognised is treated as TRANSIENT — the safe default,
    because a wrongly-retried unit costs requests while a wrongly-parked one costs data.
    """
    if isinstance(exc, (ValueError, FileNotFoundError, PermissionError)):
        return True
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return isinstance(status, int) and status in PERMANENT_STATUSES


class Worker:
    """Consumes one run's units until the chunk drains."""

    def __init__(self, queue: WorkQueue, fetcher: Fetcher, validator: Validator | None = None, name: str = "w0") -> None:
        self._q = queue
        self._fetch = fetcher
        self._validate = validator or AcceptAll()
        self._name = name

    async def _refuse(self, msg: Any, task: UnitTask, exc: Exception, outcome: ChunkOutcome) -> None:  # noqa: ANN401 — a nats Msg, typed only under TYPE_CHECKING
        """A fetch failed. Redeliver it, or park it — the two are NOT the same failure.

        `except Exception -> nak()` treated them identically, so a page that will never exist cost
        THREE fetches (`max_deliver`) against the rate-limited endpoint the queue's backpressure
        exists to protect. `storage.iiif` already fails fast on 4xx≠429 (`iiif.py:98`); the worker
        then threw that judgement away.

        Permanent means no amount of retrying changes the answer: the page is gone (404/410), we are
        not allowed to have it (401/403), or the key is not something we can fetch at all. Those park
        to the DLQ and ack, exactly like a corrupt image — one request, one verdict, named in
        `errors`. Everything else is presumed transient and redelivered.
        """
        if _is_permanent(exc):
            reason = f"permanent fetch failure: {exc}"
            outcome.errors[task.key] = reason
            await self._q.park_poison(task, reason)
            await msg.ack()
            logger.warning("unit %s parked, will not be retried: %s", task.key, exc)
            return
        await msg.nak()
        logger.warning("unit %s failed transiently, redelivering: %s", task.key, exc)

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

        **THE ACK IS PER BATCH, not per unit**, and that is the whole shape of this method. A fetched
        unit is held — fetched, validated, and NOT acked — until enough of them accumulate to be
        worth one Lance fragment. Then: one `write_fragments`, one staged manifest, and only then an
        ack for every message in the batch.

        The previous version acked per unit, which forced one fragment per image: a 10k-page volume
        produced 10k fragments in a single commit. Lance's guidance is ~1M rows per fragment, and its
        ingestion notes name the per-row write as *the* anti-pattern — "each call commits a new
        version and a new fragment".

        Holding messages unacked is what makes this safe AND what bounds the batch: the queue's
        `max_ack_pending` is the ceiling on in-flight unacked work, so `FRAGMENT_TARGET_ROWS` must
        stay under it or JetStream simply stops delivering and the drain deadlocks. The ack contract
        itself is unchanged in meaning — bytes on the store, identity staged beside them, and only
        then the ack — it now just applies to a batch instead of a row.
        """
        sub = await self._q.subscribe(run_id)
        outcome = ChunkOutcome(chunk_id=chunk_id)
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        # The open batch: fetched units and the messages still owed an ack for them.
        pending: list[tuple[str, bytes]] = []
        pending_msgs: list[Any] = []
        pending_bytes = 0

        async def flush() -> None:
            """One fragment for everything accumulated, then ack the whole batch."""
            nonlocal pending, pending_msgs, pending_bytes
            if not pending:
                return
            units, msgs_to_ack = pending, pending_msgs
            pending, pending_msgs, pending_bytes = [], [], 0

            written = write_unit_fragments(dataset_uri, units_to_table(units))
            # Staged under the FIRST unit's key: one manifest per fragment, not per row. Recovery
            # reads the staging prefix and commits whatever it finds, so the granularity only has to
            # be stable, not per-unit — and per-unit would put 10k tiny JSON objects on the store for
            # a 10k-page volume, recreating the small-file problem the fragment batching just solved.
            stage_fragments(dataset_uri, run_id, units[0][0], written)
            outcome.fragments.extend(written)
            outcome.units_done += len(units)
            for msg in msgs_to_ack:
                await msg.ack()
            logger.info("fragment: %d units, %d bytes -> %d fragment(s)", len(units), sum(len(p) for _, p in units), len(written))

        while outcome.units_done + len(pending) + len(outcome.errors) < expected:
            try:
                msgs = await sub.fetch(min(FETCH_BATCH, expected), timeout=30)
            except TimeoutError:
                # No units available right now. Not an error — another worker may hold them.
                break

            async def handle(msg: object) -> None:
                nonlocal pending_bytes
                task = UnitTask.model_validate_json(msg.data)  # type: ignore[attr-defined]
                async with sem:
                    try:
                        key, result = await self._one(task)
                    except Exception as exc:
                        await self._refuse(msg, task, exc, outcome)
                        return
                    if key is None:
                        # Validation refused it: park and ACK. Redelivering corrupt bytes cannot help.
                        outcome.errors[task.key] = str(result)
                        await self._q.park_poison(task, str(result))
                        await msg.ack()  # type: ignore[attr-defined]
                        return
                    # Held, NOT acked — the ack is owed until this unit's fragment is on the store.
                    pending.append((key, result))
                    pending_msgs.append(msg)
                    pending_bytes += len(result)

            await asyncio.gather(*(handle(m) for m in msgs))
            if len(pending) >= FRAGMENT_TARGET_ROWS or pending_bytes >= FRAGMENT_TARGET_BYTES:
                await flush()

        # The remainder. A chunk is almost never an exact multiple of the target, so without this the
        # tail of every run would be fetched, held, and then silently redelivered on ack_wait.
        await flush()

        await self._q.signal_drained(run_id, chunk_id, outcome.model_dump())
        return outcome
