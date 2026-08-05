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

from fastapi import APIRouter
from pydantic import BaseModel, Field


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
    consumers: int | None = Field(default=None, description="durable consumers bound to it")
    dlq_present: bool | None = Field(
        default=None,
        description="the DLQ stream exists. Its absence is INVISIBLE in normal operation — a parking publish with no stream fails silently, so a poison unit is dropped rather than parked.",
    )
    detail: str | None = Field(default=None, description="what went wrong, when something did")


@router.get("/queue", response_model=QueueHealth)
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
