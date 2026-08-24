"""The unit worker — fetch, validate, write a fragment, ack.

**NOT AN ORCHESTRATOR, and not a deployment.** Dapr Workflow owns the run: `workflow.ingest_run`
fans out child workflows, `when_all` fans them in, and Dapr persists and replays the history. This
class is what runs INSIDE the `drain_chunk` ACTIVITY — the JetStream consumer that does the fetching.
The division is deliberate and each half is doing the thing it is good at:

    Dapr Workflow   DURABLE DECISIONS — enumerate, fan out, fan in, commit once, emit lineage. It
                    survives a pod death because the history replays. There is no counter to
                    hand-roll and no ledger to keep.
    JetStream       IN-CHUNK DELIVERY — one unit to exactly one consumer, redelivery on a lost ack,
                    poison parking, and `max_ack_pending` backpressure. Dapr has no equivalent, and
                    an activity per page would be a million activity results in the state store.

The first version of this file DID try to be an orchestrator — the drain waited on an external Dapr
workflow event that the worker signalled over a NATS subject, with nothing bridging the two, and no
Deployment ran a `Worker` at all. Every chunk would have published its units, waited the full
ten-minute fallback, and reported zero fragments. Draining inside an activity is what fixed it.

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
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Protocol

import pyarrow as pa
from pydantic import BaseModel, Field

from ingest.lander import write_unit_fragments
from ingest.queue import ACK_WAIT_SECONDS, QueueMessage, UnitTask, WorkQueue
from ingest.sizing import ResolvedSizing, resolve
from ingest.staging import stage_fragments


if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# THREE INDEPENDENT AXES, and conflating them is what produced a fragment per image.
#
# They answer to three different systems and there is no reason for them to agree:
#
#   workflow fan-out   `workflow.CHUNK_SIZE`   — keys per CHILD WORKFLOW. A DAPR concern: a million
#                                                activity results would melt the state store. Nothing
#                                                to do with Lance.
#   source politeness  `sizing.fetch_*`        — in-flight requests against the SOURCE. A IIIF/HCP
#                                                concern: rate limits, not throughput.
#   storage layout     `sizing.fragment_*`     — rows per LANCE FRAGMENT. A Lance concern.
#
# The third one did not exist. Every unit was written as its own fragment
# (`units_to_table([(key, result)])` — a list of ONE), so a 10k-page volume produced 10k fragments in
# a single commit, 10k staging manifests, and 10k FragmentMetadata blobs across a Dapr boundary.
# Lance's own guidance is ~1M rows per fragment, and its ingestion notes name the per-row write as
# the anti-pattern: "each call commits a new version and a new fragment".
#
# The last two are NOT module constants any more — they live on the run, resolved at accept from the
# request and defaulted from env (`ingest.sizing`). They were deployment-wide, which is one value for
# every source the plane would ever ingest; a rate-limited IIIF endpoint and an S3 bucket in the same
# datacentre want different numbers, and only the caller knows which they are hitting.

#: How often a HELD message tells JetStream it is still being worked. A fifth of the queue's
#: `ACK_WAIT_SECONDS`, so several consecutive misses still do not expire the ack — derived from that
#: constant rather than written as a literal, because the two drifting apart is invisible: the only
#: symptom is the queue quietly redelivering LIVE work, which reads as a slow source and lands as a
#: staging overlap.
HEARTBEAT_SECONDS = ACK_WAIT_SECONDS / 5


async def _renew_held(held: dict[int, Any]) -> None:
    """Tell JetStream the held messages are still being worked, until cancelled.

    `msg.in_progress()` was called NOWHERE in this plane, and a batch holds its messages unacked from
    first fetch until the fragment is on the store. Against a slow or rate-limited source that window
    exceeds `ACK_WAIT_SECONDS` (300) and JetStream redelivers units a LIVE worker is still holding —
    so the same bytes are fetched twice against the very endpoint the concurrency limit exists to
    protect, and the two workers' fragments overlap in a way `discover_staged` may not resolve.

    No crash required, which is what made this worse than the documented overlap: an ordinary slow
    run reaches it.

    Takes the held MAPPING rather than closing over the batch list, because `flush` REBINDS
    `pending_msgs` to a fresh list — a heartbeat closing over that name would keep renewing the
    previous batch while the current one expired.

    Keyed by `id(msg)` and NOT a set: a nats-py `Msg` is UNHASHABLE, so `held.add(msg)` raised
    `TypeError: unhashable type: 'Msg'` inside the drain activity — which Dapr reported as a failed
    activity, leaving the run COMPLETE with `units_done: 0` and nothing committed. The unit test
    missed it because its fake message was a plain object, which IS hashable; the in-cluster lane
    caught it on the first run. Identity is the right key anyway (two messages are never "equal"),
    and `id()` cannot be recycled while the mapping holds the reference.
    """
    # POLL REASON: KEEPALIVE, not a poll — this loop TELLS and never asks. It reports "still working"
    # to JetStream for messages this process is holding, and reads no state, so there is nothing an
    # event could deliver instead: the fact being reported is our own liveness, and only we have it.
    # A13 forbids polling loops (a loop asking "is it done yet"); an ack keepalive is the opposite,
    # and without it `ack_wait` expires and the queue redelivers work a LIVE worker is mid-fetch on.
    # BOUNDED by the drain: created in `drain_chunk` and cancelled in its `finally`, so it cannot
    # outlive the work it accompanies.
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        for msg in list(held.values()):
            with contextlib.suppress(Exception):
                # Best-effort per message: one already-acked or expired message must not stop the
                # others being renewed.
                await msg.in_progress()


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


def units_to_table(
    units: Sequence[tuple[str, bytes]],
    partitions: Sequence[str | None] | None = None,
    tokens: Sequence[str | None] | None = None,
    external_base: str | None = None,
) -> pa.Table:
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

    **`external_base` chooses the PLACEMENT** (`open_data_spec.md` §4.1, change 1). With a base, the
    payload column stores an EXTERNAL descriptor — the source URI — and lance copies nothing: 0.1% of
    the corpus on disk against 100.1% for the managed form, and the descriptor still resolves after
    being carried into silver and gold, which is what stops the estate holding three copies
    (`scripts/measure_external_blob_carry_forward.py`).

    The keys ARE the URIs — `source_uri` below is the same list — so this is a placement change, not
    a data change: bronze remains faithful to source either way. The bytes are still FETCHED, because
    validation and the `sha256` fixity column both read them; fetching to hash is not copying into
    the lakehouse, and nothing is written twice.

    Without a base the bytes are stored, which is correct for a kind whose payload exists at no URI
    (`lance-append` synthesises Arrow IPC from fragments) and for a source whose lifecycle is not
    the estate's.

    `partitions` is positional-parallel to `units` and OPTIONAL: omitted (or None entries) writes
    nulls, which is what a source with no meaningful grouping should produce. The values are the
    ADAPTER's — see `sources.partition_key_for` — and this function deliberately does not derive them
    from the key, because that would put source knowledge in the worker.
    """
    import hashlib

    from lance import blob_array
    from lance.blob import Blob

    from ingest.identity import unit_id
    from ingest.runtime import BRONZE_SCHEMA, BRONZE_STAGE

    etags = list(tokens) if tokens is not None else [None] * len(units)
    if len(etags) != len(units):
        raise ValueError(f"tokens has {len(etags)} entries for {len(units)} units")
    # THE identity, from the ONE derivation (`identity.unit_id`) the enumerate anti-join also
    # uses. An inline copy here and another there is how enumerate skips a row the worker would
    # have written — silently, which is the worst available failure.
    ids = [unit_id(k, tok) for (k, _), tok in zip(units, etags, strict=True)]
    keys = list(partitions) if partitions is not None else [None] * len(units)
    if len(keys) != len(units):
        # Positional parallelism is the contract; a length mismatch would silently misattribute rows
        # to the wrong partition, which is worse than writing none.
        raise ValueError(f"partitions has {len(keys)} entries for {len(units)} units")
    return pa.table(
        {
            "id": pa.array(ids, pa.int64()),
            "source_uri": pa.array([k for k, _ in units], pa.string()),
            # EXTERNAL when a base is registered, MANAGED otherwise. `Blob.from_uri` with no
            # position/size means the WHOLE object — passing a zero size instead asks for a
            # zero-length slice and yields an empty read with no error, which is the silent failure
            # this estate keeps producing.
            "payload": blob_array([Blob.from_uri(k) for k, _ in units] if external_base else [p for _, p in units]),
            # Fixity (#99): over the PAYLOAD as fetched — hashed here, before the write, never read
            # back from Lance afterwards. Distinct from `id`, which hashes the KEY: one names the
            # row, the other asserts the bytes. See the schema comment in runtime.py.
            "sha256": pa.array([hashlib.sha256(p).hexdigest() for _, p in units], pa.string()),
            # The tier stamp, written at ingest because bronze is the first GOVERNED tier (R23).
            # Not decoration: the media viewer PROJECTS it, so a bronze table without it is one no
            # reader in the estate can open — see the schema comment.
            "stage": pa.array([BRONZE_STAGE] * len(units), pa.string()),
            # The listing fingerprint this identity was derived FROM — recorded so the anti-join
            # and an auditor can see which version of the source object this row witnessed.
            "etag": pa.array(etags, pa.string()),
            # How this row GROUPS — a column, not a fragment boundary. See BRONZE_SCHEMA for the
            # measurement that decided that (compaction merges key-pure fragments; a column survives).
            "partition_key": pa.array(keys, pa.string()),
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


def _is_redelivery(msg: QueueMessage) -> bool:
    """Has JetStream handed us this unit before?

    `num_delivered` is 1 on a first delivery and climbs on every redelivery, so >1 means some earlier
    attempt did not ack — a crash, an `ack_wait` expiry, or a nak. Those units must be batched apart
    from fresh ones (see `drain_chunk`).

    Unknown counts as fresh: a test double or a non-JetStream message has no metadata, and treating
    that as a redelivery would flush a fragment per message and undo the batching. The cost of being
    wrong in this direction is bounded — `discover_staged` still refuses to commit an ambiguous
    overlap rather than duplicating rows.
    """
    try:
        return int(msg.metadata.num_delivered) > 1
    except (AttributeError, TypeError, ValueError):
        return False


class Worker:
    """Consumes one run's units until the chunk drains."""

    def __init__(
        self,
        queue: WorkQueue,
        fetcher: Fetcher,
        validator: Validator | None = None,
        name: str = "w0",
        sizing: ResolvedSizing | None = None,
    ) -> None:
        self._q = queue
        self._fetch = fetcher
        self._validate = validator or AcceptAll()
        self._name = name
        # The RUN's numbers, resolved at accept and carried down through the chunk spec. Falling back
        # to `resolve()` here (deployment defaults) rather than requiring them keeps every existing
        # caller — and every test that builds a bare Worker — working unchanged.
        self._sizing = sizing or resolve()

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

    async def drain_chunk(self, run_id: str, chunk_id: str, expected: int, dataset_uri: str, external_base: str | None = None) -> ChunkOutcome:
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
        `max_ack_pending` is the ceiling on in-flight unacked work, so the run's `fragment_rows` must
        stay under it or JetStream simply stops delivering and the drain deadlocks. The ack contract
        itself is unchanged in meaning — bytes on the store, identity staged beside them, and only
        then the ack — it now just applies to a batch instead of a row.
        """
        sub = await self._q.subscribe(run_id)
        outcome = ChunkOutcome(chunk_id=chunk_id)
        sizing = self._sizing
        sem = asyncio.Semaphore(sizing.fetch_concurrency)

        # The open batch: fetched units and the messages still owed an ack for them.
        pending: list[tuple[str, bytes]] = []
        # Positional-parallel to `pending`: the ADAPTER's grouping label for each held unit, taken
        # off the task rather than derived here — the worker resolves units by URI scheme and does
        # not know what a volume or a folder is.
        pending_parts: list[str | None] = []
        pending_tokens: list[str | None] = []
        pending_msgs: list[Any] = []
        pending_bytes = 0

        # EVERY message currently held unacked, in a container that is never REASSIGNED — `flush`
        # rebinds `pending_msgs` to a fresh list, so a heartbeat closing over that name would keep
        # renewing the previous batch while the current one expires.
        #
        # A DICT KEYED BY `id`, not a set: a nats-py `Msg` is unhashable.
        held: dict[int, Any] = {}

        async def flush() -> None:
            """One fragment for everything accumulated, then ack the whole batch."""
            nonlocal pending, pending_msgs, pending_bytes, pending_parts, pending_tokens
            if not pending:
                return
            units, msgs_to_ack, parts, toks = pending, pending_msgs, pending_parts, pending_tokens
            pending, pending_msgs, pending_bytes, pending_parts, pending_tokens = [], [], 0, [], []

            written = write_unit_fragments(dataset_uri, units_to_table(units, parts, toks, external_base))
            # EVERY unit in the batch, not `units[0][0]`. The manifest is the finalizer's only record
            # of which rows a fragment holds, so naming one member left the other N-1 invisible and a
            # partially-acked batch committed its units TWICE
            # (`tests/test_partial_ack_duplication.py`). Still one manifest — it is keyed on the set.
            stage_fragments(dataset_uri, run_id, [key for key, _ in units], written)
            outcome.fragments.extend(written)
            outcome.units_done += len(units)
            for msg in msgs_to_ack:
                await msg.ack()
                held.pop(id(msg), None)
            logger.info("fragment: %d units, %d bytes -> %d fragment(s)", len(units), sum(len(p) for _, p in units), len(written))

        beat = asyncio.create_task(_renew_held(held))
        try:
            while outcome.units_done + len(pending) + len(outcome.errors) < expected:
                try:
                    msgs = await sub.fetch(min(sizing.fetch_batch, expected), timeout=30)
                except TimeoutError:
                    # No units available right now. Not an error — another worker may hold them.
                    break

                async def handle(msg: QueueMessage) -> None:
                    nonlocal pending_bytes
                    task = UnitTask.model_validate_json(msg.data)  # type: ignore[attr-defined]
                    async with sem:
                        try:
                            # Kept as ONE value rather than unpacked: `_one` returns
                            # `tuple[str, bytes] | tuple[None, str]`, and the two halves are
                            # correlated. Unpacking first breaks that — the refusal branch's `str`
                            # then rides along into the payload list, which is a `bytes` list.
                            fetched = await self._one(task)
                        except Exception as exc:
                            await self._refuse(msg, task, exc, outcome)
                            return
                        if fetched[0] is None:
                            # Validation refused it: park and ACK. Redelivering corrupt bytes cannot help.
                            reason = fetched[1]
                            outcome.errors[task.key] = reason
                            await self._q.park_poison(task, reason)
                            await msg.ack()
                            return
                        # Held, NOT acked — the ack is owed until this unit's fragment is on the store.
                        key, payload = fetched
                        pending.append((key, payload))
                        pending_parts.append(task.partition_key)
                        pending_tokens.append(task.token)
                        pending_msgs.append(msg)
                        held[id(msg)] = msg
                        pending_bytes += len(payload)

                # A REDELIVERED unit never shares a fragment with a fresh one, and that isolation is what
                # keeps recovery decidable. `discover_staged` resolves an overlap by dropping the
                # fragment whose units another already covers — which only works while overlaps are
                # containments. Let a redelivery batch with fresh units and you get F={u0..u3} against
                # H={u2,u3,u4,u5}: neither contains the other, so committing either is wrong and
                # `discover_staged` can only raise. Splitting the fetch is what makes that unreachable.
                fresh = [m for m in msgs if not _is_redelivery(m)]
                again = [m for m in msgs if _is_redelivery(m)]

                if fresh:
                    await asyncio.gather(*(handle(m) for m in fresh))
                if again:
                    await flush()  # close the fresh batch before the redelivered units join it
                    # ONE FRAGMENT PER REDELIVERED UNIT — what makes the overlap state STRUCTURALLY
                    # unreachable rather than merely reported.
                    #
                    # Splitting fresh from redelivered (above) stops a redelivery merging with fresh
                    # work, but left redeliveries batching with EACH OTHER — and they do not
                    # necessarily come from the same original batch. Two crashed batches whose
                    # remainders arrive in one fetch produced a single fragment overlapping both and
                    # contained by neither: F={u0..u3} against H={u2,u3,u4,u5}, which no selection
                    # resolves, so `discover_staged` could only raise and strand the run.
                    #
                    # A SINGLETON is contained by any fragment holding it, so the staged family stays
                    # LAMINAR — every pair nested or disjoint — and an exact cover always exists.
                    #
                    # Deliberately NOT "batch all uncovered redeliveries together", which looks
                    # equivalent and is not race-proof: worker A holds {u0..u5}, ack_wait expires on
                    # u0..u3, worker B batches {u0,u1,u2,u3,u9} — F\\H={u4,u5} and H\\F={u9}, neither
                    # contained, and the state is back.
                    #
                    # The cost is one fragment per redelivered unit, paid only on recovery; the
                    # heartbeat above is what keeps that population small in the first place.
                    for msg in again:
                        await handle(msg)
                        await flush()
                elif len(pending) >= sizing.fragment_rows or pending_bytes >= sizing.fragment_bytes:
                    await flush()

        finally:
            # ALWAYS. A heartbeat that outlives its drain keeps renewing messages nobody is working,
            # which is the same redelivery-starvation this exists to prevent, pointed the other way.
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

        # The remainder. A chunk is almost never an exact multiple of the target, so without this the
        # tail of every run would be fetched, held, and then silently redelivered on ack_wait.
        await flush()

        # RETURNING the outcome is the signal. There was a `signal_drained` publish here, left over
        # from the design where a chunk workflow suspended on an external NATS event — which nothing
        # in the estate ever raised. Draining inside the activity replaced it: Dapr persists this
        # return value and replays the activity if the pod dies, so a second, unacked notification on
        # a WORK_QUEUE stream bought nothing and accumulated forever.
        return outcome
