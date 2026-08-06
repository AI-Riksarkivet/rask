"""The unit work queue — the ONE module permitted to import `nats` (invariant I3).

Everything else in the plane publishes events through Dapr pub/sub, which is what keeps the broker a
chart value rather than a code dependency. This module is the documented exception, and
`tests/unit/test_ingest_invariants.py` counts it: an exception stays an exception only if something
enforces the boundary.

**Why a direct client here and Dapr everywhere else.** The unit queue needs semantics Dapr's pubsub
component cannot express, and every one of them is load-bearing:

* `RetentionPolicy.WORK_QUEUE` — a message is REMOVED once acked. That is what makes the stream
  itself the outstanding-work ledger, which is why this plane needs no side ledger (the reasoning
  that dissolved the tracker, docs/DECISIONS.md).
* `max_ack_pending` — bounds in-flight units per worker, i.e. backpressure against a rate-limited
  IIIF endpoint. Without it a worker fetches faster than it can land and the source throttles us.
* `ack_wait` + explicit `nak(delay)` — a unit that fails transiently is redelivered on OUR schedule,
  not on a global component default.
* batch `fetch()` — one round trip per N units instead of per unit, across millions of units.

**M4 is why the DLQ path matters.** The estate's Dapr resiliency retry window is real again
(constant 90s x 5 = 450s, Phase 0), but a poison unit must still leave the queue rather than
redeliver forever — hence `max_deliver` and the dlq.ingest.tasks parking subject.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any, Protocol, Self, TypedDict, runtime_checkable

import nats
from nats.js import api as jsapi
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from nats.aio.client import Client as NatsClient
    from nats.js import JetStreamContext

STREAM = "INGEST"
SUBJECT_ROOT = "ingest"
DLQ_SUBJECT = "dlq.ingest.tasks"

# One unit may hold the ack this long before JetStream assumes the worker died. Generous because a
# unit is an HTTP fetch of a multi-MB page image from a rate-limited external endpoint; too short and
# a slow-but-alive worker has its work redelivered underneath it, which duplicates fetches against
# the very endpoint we are trying not to hammer.
ACK_WAIT_SECONDS = 300

# Bounded in-flight work per worker — backpressure, not a performance knob.
#
# MUST exceed the run's `sizing.fragment_rows`. The worker holds a whole fragment's worth of messages
# UNACKED while it accumulates them, so a ceiling below the batch size makes JetStream stop
# delivering at the ceiling and the drain waits forever for units it will never be sent — a deadlock
# that looks exactly like a slow source. At 32 (the old value) against a 1024-row batch it would have
# hung on the 33rd unit of every run.
MAX_ACK_PENDING = int(os.getenv("RASK_INGEST_MAX_ACK_PENDING", "2048"))

# After this many deliveries a unit is poison: it parks on the DLQ subject and the run completes
# WITH ERRORS rather than hanging. A run that never finishes is worse than one that reports what
# refused to land.
MAX_DELIVER = 3


class UnitTask(BaseModel):
    """One unit of ingest work. Deliberately a REFERENCE, never the bytes.

    Page images are 1-20 MB and NATS defaults to a 1 MB max payload; bytes move
    source -> object store -> Lance over HTTP/S3 and never touch the bus. The task carries what the
    worker needs to go and get them.
    """

    run_id: str
    chunk_id: str
    key: str = Field(description="the unit's stable key — the source URI")
    dataset_uri: str
    traceparent: str | None = Field(default=None, description="W3C trace context, so one run's trace spans api -> workers -> lander")
    #: How this unit GROUPS — the bronze `partition_key` column. Computed by the ADAPTER at
    #: enumeration (`sources.partition_key_for`) and carried, because the worker resolves units by
    #: URI scheme and must not learn what a volume or a prefix is. Optional: a kind that registers no
    #: `partition_of` sends nothing and the column is written null.
    partition_key: str | None = None


def unit_subject(run_id: str) -> str:
    return f"{SUBJECT_ROOT}.tasks.{run_id}"


#: How long JetStream remembers a `Nats-Msg-Id` and refuses a duplicate. Matches the chart's
#: `--dupe-window=2m` on the INGEST stream — the two must agree, because whichever creates the stream
#: first wins and the other silently accepts the difference.
DUPLICATE_WINDOW = 120.0


def _dedupe_id(task: UnitTask) -> str:
    """A stable message id for one unit of one run — `sha256(run_id \\x00 key)`, hex.

    HASHED rather than `f"{run_id}:{key}"` for two reasons that both bite in practice: a source key
    is a URI and can be long, while a NATS header value must not contain CR or LF — a key carrying
    either would corrupt the header frame rather than fail loudly. The hash is deterministic, which
    is the whole requirement: the SAME unit replayed by the SAME run must produce the SAME id.

    `chunk_id` is deliberately NOT in the id. It is the enumeration chunk, and a replay re-derives it
    identically today — but including it would make the id depend on how enumeration happened to
    batch, which is not part of a unit's identity.
    """
    import hashlib

    return hashlib.sha256(f"{task.run_id}\\x00{task.key}".encode()).hexdigest()


class WorkQueue:
    """Publish and consume unit tasks over JetStream."""

    def __init__(self, nc: NatsClient, js: JetStreamContext) -> None:
        self._nc = nc
        self._js = js

    @classmethod
    async def connect(cls, servers: str | list[str], **options: Any) -> Self:  # noqa: ANN401
        nc = await nats.connect(servers, **options)
        return cls(nc, nc.jetstream())

    async def ensure_stream(self) -> None:
        """Create the INGEST stream if the chart's provisioning Job has not yet.

        Idempotent, and deliberately WORK_QUEUE retention: Dapr does NOT auto-create streams, and a
        publish with no stream FAILS. The chart's nats-stream-job owns this in-cluster; this exists
        so a test or a local run is not blocked on a Job.
        """
        config = jsapi.StreamConfig(
            name=STREAM,
            subjects=[f"{SUBJECT_ROOT}.>"],
            retention=jsapi.RetentionPolicy.WORK_QUEUE,
            # Explicit, because `publish_units` DEPENDS on it. NATS defaults this to 2 minutes and
            # the chart's Job asks for the same, but a default that a correctness property rests on
            # should be stated where the property is — otherwise a future stream tweak silently turns
            # the dedupe off and the only symptom is duplicate rows under crash recovery.
            duplicate_window=DUPLICATE_WINDOW,
        )
        try:
            await self._js.add_stream(config)
        except Exception:
            return

    async def publish_units(self, tasks: Sequence[UnitTask]) -> int:
        """Publish a chunk's units, DEDUPED on a deterministic message id. Returns the count accepted.

        `workflow.publish_units` has always documented this — "JetStream dedupes on the message id
        within the stream's duplicate window, and a unit's id is derived from (run, key) — both
        stable" — and it was never implemented: no header was set here, and `Nats-Msg-Id` appeared
        nowhere in the repo. A replayed activity therefore re-queued its ENTIRE chunk as brand-new
        messages, which `_is_redelivery` cannot see (they are first deliveries of new messages), so
        the same unit was fetched and staged twice under two different fragments. Dapr replays an
        activity whose result was not durably recorded before the pod died, so this is an ordinary
        crash-recovery path, not an exotic one.

        The stream's dedupe window is what bounds it — `--dupe-window=2m` in the chart's
        nats-stream-job, and `DUPLICATE_WINDOW` below for a locally-created stream. Beyond that
        window a replay does re-queue, and the staging layer's exact-cover selection is what absorbs
        it; this shrinks that surface rather than removing it.
        """
        published = 0
        for task in tasks:
            await self._js.publish(
                unit_subject(task.run_id),
                task.model_dump_json().encode(),
                headers={"Nats-Msg-Id": _dedupe_id(task)},
            )
            published += 1
        return published

    async def subscribe(self, run_id: str) -> JetStreamContext.PullSubscription:
        """The run's SHARED durable pull consumer — every worker pulls from this one.

        ONE durable per run, NOT one per worker. A work-queue stream permits exactly one consumer
        per filter subject; a second filtered consumer on the same subject is refused outright with
        `filtered consumer not unique on workqueue stream` (err_code 10100, reproduced against the
        estate's own NATS). An earlier version of this took a `worker_name` and minted a durable per
        worker — it passed with a single worker and would have failed the moment the plane scaled to
        two, which is the only configuration that matters.

        Competing consumers is what the shared durable already gives: JetStream hands each pending
        message to exactly one puller, so scaling is adding pods and nothing has to partition the
        work. Durable and named per RUN, so a restarted worker re-attaches to the run's position
        rather than replaying it from the beginning.
        """
        return await self._js.pull_subscribe(
            unit_subject(run_id),
            durable=f"ingest-{run_id}".replace(".", "-")[:64],
            config=jsapi.ConsumerConfig(
                ack_wait=ACK_WAIT_SECONDS,
                max_ack_pending=MAX_ACK_PENDING,
                max_deliver=MAX_DELIVER,
            ),
        )

    async def release_run(self, run_id: str) -> int:
        """Drop everything this run left on the queue. Returns the number of units released.

        THE STRAND THIS CLOSES, and it is not the one it looks like. A run publishes its units
        (`publish_units`) and drains them in a LATER activity — and `drain_chunk` is what creates the
        durable. So a run that dies in between leaves units behind with a consumer that was never
        BORN, which is why the live estate sat at `messages: 1, consumers: 0` for hours. Nothing was
        lost from a consumer; nothing had ever been bound.

        WORK_QUEUE retention is what makes it permanent: a message leaves only when it is ACKED, no
        consumer will ever be created for that run id again, and nothing sweeps the stream. Every
        other signal stays green.

        Called from the workflow's TERMINAL path, which is the whole point. A sweep would have to
        re-derive "this run has no live workflow" from outside — the same inference the `stranded`
        flag exists because two readers got wrong. The workflow does not infer: it knows it is
        ending, and it releases what it published.

        Both operations are best-effort and logged, never raised. This runs while a run is already
        terminating, and a failure to tidy up must not turn a run that landed its data into a run that
        failed — the same reasoning as I8 for lineage.
        """
        released = 0
        try:
            info = await self._js.purge_stream(STREAM, subject=unit_subject(run_id))
            released = int(getattr(info, "purged", 0) or 0)
        except Exception:
            logger.warning("could not purge %s for run %s — units may remain queued", STREAM, run_id, exc_info=True)

        # The durable too, and AFTER the purge: deleting it first would leave the messages with
        # nothing bound and no name to find them by. Absent is the normal case — a run that died
        # before its drain never created one.
        with contextlib.suppress(Exception):
            await self._js.delete_consumer(STREAM, f"ingest-{run_id}".replace(".", "-")[:64])

        if released:
            logger.info("released %d undrained unit(s) for run %s", released, run_id)
        return released

    async def park_poison(self, task: UnitTask, reason: str) -> None:
        """Park a unit that exhausted its deliveries, so it is visible rather than merely gone."""
        await self._js.publish(
            DLQ_SUBJECT,
            json.dumps({"task": task.model_dump(), "reason": reason}).encode(),
        )

    # `signal_drained` was here, and it LEAKED. It published to `ingest.run.{run}.drained` to wake a
    # chunk workflow that suspended on an external event — the design `worker.py`'s module docstring
    # records as already replaced, because nothing in the estate ever raised that event and no
    # Deployment ran a `Worker` at all. Moving the drain INTO the activity made the signal redundant:
    # the activity's RETURN VALUE is how the workflow learns its chunk is done, and Dapr persists it.
    #
    # Deleting it rather than leaving it dead, because it was not merely unused — it was accumulating.
    # `ingest.run.*.drained` matches this stream's own `ingest.>` filter, and the stream is WORK_QUEUE
    # retention, where a message is removed only when it is ACKED. `grep signal_drained services/`
    # found no consumer anywhere, so every chunk of every run published one message that nothing would
    # ever ack, and they piled up on the stream forever.
    #
    # An unread message on a work queue is not a feature, and a "signal" nobody listens for is worse
    # than no signal: it reads as a working mechanism.

    async def close(self) -> None:
        await self._nc.close()


@runtime_checkable
class QueueMessage(Protocol):
    """One delivered unit, as everything OUTSIDE this seam is allowed to see it.

    I3 confines the broker client to this module, which left the worker annotating its messages
    `msg: object` and then reaching for `.data`, `.ack()`, `.metadata` behind a suppression each — so
    the one place the plane touches broker semantics was the one place with no types at all. A
    `TYPE_CHECKING` import of `nats.aio.msg` is not the way out: the I3 gate is a regex over import
    lines and would refuse it, correctly, because the point of the rule is that the bus stays
    swappable by chart values rather than by a code change.

    A structural Protocol is the honest shape of that rule. The seam publishes the CONTRACT, the
    worker depends on the contract, and a different broker satisfies it or is caught at the boundary
    instead of at the fifth attribute access.
    """

    @property
    def data(self) -> bytes: ...

    @property
    def metadata(self) -> Any: ...  # noqa: ANN401 — broker-specific; only `num_delivered` is read, defensively

    async def ack(self) -> None: ...

    async def nak(self, delay: float | None = None) -> None: ...

    async def in_progress(self) -> None: ...


class QueueSnapshot(TypedDict):
    """The probe's result, typed PER KEY so the health endpoint can splat it and stay checkable.

    `dict[str, object]` made `QueueHealth(**snapshot)` unverifiable: every field arrived as `object`,
    so ty reported one error per field — and, far worse, would have reported nothing at all if the
    probe started returning `messages` as a string. The endpoint's entire contract is that these five
    values mean what they say, and an untyped bag is exactly how that stops being checked.
    """

    reachable: bool
    stream_present: bool
    messages: int | None
    consumers: int | None
    dlq_present: bool


async def inspect_queue(url: str, timeout: float = 3.0) -> QueueSnapshot:
    """What the work queue looks like right now — reachability, stream presence, depth.

    LIVES HERE, not in the health endpoint, because invariant I3 confines the broker client to this
    seam: "only the work queue may hold the broker client, or the bus stops being swappable by chart
    values." A diagnostic that imported `nats` directly would put a second nats-py dependency outside
    the seam — and the I3 gate caught exactly that.

    Wrapped in `asyncio.wait_for` by the CALLER's deadline rather than trusting the client: measured,
    a connect to a dead address with `connect_timeout`, `allow_reconnect=False` and
    `max_reconnect_attempts=0` all set had still not returned after 60 seconds.
    """
    import nats

    nc = None
    try:
        nc = await nats.connect(url, connect_timeout=timeout, allow_reconnect=False, max_reconnect_attempts=0)
        js = nc.jetstream()
        out = QueueSnapshot(reachable=True, stream_present=False, messages=None, consumers=None, dlq_present=False)
        try:
            info = await js.stream_info(STREAM)
            out.update(stream_present=True, messages=info.state.messages, consumers=info.state.consumer_count)
        except Exception:
            # An absent stream is the ANSWER, not an error — and it is the answer that hid for four
            # and a half hours while every liveness probe stayed green.
            logger.warning("the %s stream does not exist — publishes will fail until it is provisioned", STREAM)
        try:
            await js.stream_info(DLQ_SUBJECT.split(".")[0].upper())
            out["dlq_present"] = True
        except Exception:
            out["dlq_present"] = False
            logger.warning("the DLQ stream does not exist — parked poison units are DROPPED, not parked")
        return out
    finally:
        if nc is not None:
            with contextlib.suppress(Exception):
                await nc.close()
