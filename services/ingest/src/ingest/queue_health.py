"""`GET /queue` — what the work queue actually looks like right now.

WHY THIS EXISTS, and it is not a nicety. On 2026-08-05 a NATS restart re-formed the JetStream
metadata group from two fresh peers, and EVERY stream in the estate disappeared — LINEAGE, MEDALLION,
TRAINING, DLQ and INGEST. `nats stream ls` answered "No Streams defined". Nothing detected it:

  * liveness stayed green, correctly — it probes the process, not its dependencies
  * the ingest plane's own `ensure_stream` silently recreated INGEST on the next publish, so the
    plane looked fine while four other streams stayed missing
  * Dapr does NOT auto-create streams, so every publish to those four would have failed — and a
    parking publish with no stream fails SILENTLY, un-parking the message it was meant to save

The outage was found only because someone ran `stream ls` by hand while debugging something else.

So this endpoint answers the question an operator actually has — "is the queue there, and what is on
it" — and it answers it from the plane that owns the queue. Deliberately NOT folded into
`/health`: that route is liveness, and a liveness that fails when NATS is down turns one broken
dependency into a restart loop across every pod that touches it, which is how a partial outage
becomes a total one. This REPORTS; it never gates.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Self

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator


logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

#: A queue probe must not inherit the latency of the thing it is reporting on — an operator reaches
#: for this endpoint precisely when something is already slow.
PROBE_TIMEOUT_SECONDS = 3.0


class QueueHealth(BaseModel):
    """The work queue as the ingest plane sees it.

    `reachable` and `stream_present` are separate on purpose: "NATS is down" and "NATS is up but the
    stream this plane needs does not exist" are different incidents with different fixes, and the
    second is the one that hid for four and a half hours.
    """

    reachable: bool = Field(description="the NATS server answered")
    stream_present: bool | None = Field(default=None, description="the INGEST stream exists; None when unreachable")
    messages: int | None = Field(default=None, description="messages currently on the stream")
    consumers: int | None = Field(
        default=None,
        description=(
            "durable consumers bound to the stream RIGHT NOW. Zero is the normal IDLE state, not a "
            "fault: the drain creates one durable per run (`ingest-<run_id>`) and it goes away with "
            "the run, so between runs there is nothing bound. Read `stranded` for the question this "
            "number gets mistaken for."
        ),
    )
    dlq_present: bool | None = Field(
        default=None,
        description=(
            "the DLQ stream exists. Its absence is INVISIBLE in normal operation — a parking publish "
            "with no stream fails silently, so a poison unit is dropped rather than parked."
        ),
    )
    retention: str | None = Field(
        default=None,
        description=(
            "the live stream's retention policy, because `messages` means two different things. Under "
            "WORK_QUEUE a message leaves when ACKED, so depth IS outstanding work; under `limits` an "
            "acked message is retained for max_age and depth is not. The chart creates INGEST with "
            "`limits` while this plane is written against WORK_QUEUE, and nothing said which you had."
        ),
    )
    stranded: bool | None = Field(
        default=None,
        description=(
            "messages are queued and NOTHING is bound to take them — work nobody is coming for. "
            "The endpoint already held both numbers and reported neither, so two readers in one day "
            "read `consumers: 0` as 'no worker exists' when it is the ordinary idle state."
        ),
    )
    detail: str | None = Field(default=None, description="what went wrong, when something did")

    @model_validator(mode="after")
    def _derive_stranded(self) -> Self:
        """`messages > 0` with `consumers == 0` is the one combination that means WORK IS LOST.

        The stream is WORK_QUEUE retention, so a message leaves only when it is ACKED. A unit whose
        run died takes its durable consumer with it and the message then sits forever: no consumer
        will be created for that run id again, nothing sweeps the stream, and every other signal
        stays green. Measured on the live estate — one message, no consumers, unchanged across an
        hour.

        Derived rather than reported by the probe because it is a JUDGEMENT about two facts, and the
        probe's job is to state facts. Computed here, once, so no caller has to remember the rule —
        which is exactly what both readers of this endpoint failed to do.
        """
        if self.reachable and self.messages is not None and self.consumers is not None:
            object.__setattr__(self, "stranded", self.messages > 0 and self.consumers == 0)
        return self


@router.get("/queue")
async def queue_health() -> QueueHealth:
    """Report the queue's state. Always 200 — this is a diagnostic, not a gate.

    A non-200 would make the endpoint useless for its own purpose: the operator asking WHY the queue
    is broken would get a failure instead of the diagnosis.

    The probe itself lives in `ingest.queue` because invariant I3 confines the broker client to that
    seam — an earlier version imported `nats` here and the I3 gate caught it. The DEADLINE is applied
    here, where the endpoint's contract is: measured, a nats connect to a dead address does NOT honour
    its own `connect_timeout` (still unreturned after 60s, and the suite went 21s -> 362s), so the
    only reliable bound is `asyncio.wait_for` around the whole thing.
    """
    from ingest.queue import inspect_queue
    from ingest.runtime import nats_url

    try:
        return QueueHealth(**await asyncio.wait_for(inspect_queue(nats_url(), PROBE_TIMEOUT_SECONDS), timeout=PROBE_TIMEOUT_SECONDS))
    except TimeoutError:
        return QueueHealth(reachable=False, detail=f"the queue did not answer within {PROBE_TIMEOUT_SECONDS}s")
    except Exception as exc:
        return QueueHealth(reachable=False, detail=f"{type(exc).__name__}: {exc}")
