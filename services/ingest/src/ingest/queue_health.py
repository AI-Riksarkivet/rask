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

    A non-200 here would make the endpoint unusable for its own purpose: an operator checking WHY the
    queue is broken would get a failure instead of the diagnosis.

    The whole probe is wrapped in `asyncio.wait_for`, and that is the SECOND attempt at bounding it.
    The first passed `connect_timeout` (plus `allow_reconnect=False`, `max_reconnect_attempts=0`) to
    the nats client and assumed that bounded the call. It does not: MEASURED against a dead address,
    a connect with all three set still had not returned after 60 seconds, and the test suite went
    from 21s to 362s. Do not trust a client's own timeout to bound an operation you must answer
    within — bound it here, where the deadline is the contract.
    """
    from ingest.queue import DLQ_SUBJECT, STREAM
    from ingest.runtime import nats_url

    try:
        import nats
    except Exception as exc:  # pragma: no cover - import guard
        return QueueHealth(reachable=False, detail=f"nats client unavailable: {exc}")

    async def _probe() -> QueueHealth:
        nc = None
        try:
            nc = await nats.connect(
                nats_url(),
                connect_timeout=PROBE_TIMEOUT_SECONDS,
                allow_reconnect=False,
                max_reconnect_attempts=0,
            )
            js = nc.jetstream()

            present, messages, consumers = False, None, None
            try:
                info = await js.stream_info(STREAM)
                present, messages, consumers = True, info.state.messages, info.state.consumer_count
            except Exception:
                # NOT an error path: an absent stream is the ANSWER, and it is the answer that was
                # missing for four and a half hours. Reported as `stream_present: false`.
                logger.warning("the %s stream does not exist — publishes will fail until it is provisioned", STREAM)

            # The DLQ is checked separately because its absence is the quietest failure in the plane:
            # `park_poison` publishes and moves on, so with no stream a corrupt unit is DROPPED
            # rather than parked, and the run still reports the error. Nothing else would notice.
            dlq_stream = DLQ_SUBJECT.split(".")[0].upper()
            try:
                await js.stream_info(dlq_stream)
                dlq_present = True
            except Exception:
                dlq_present = False
                logger.warning("the %s stream does not exist — parked poison units are DROPPED, not parked", dlq_stream)

            return QueueHealth(reachable=True, stream_present=present, messages=messages, consumers=consumers, dlq_present=dlq_present)
        finally:
            if nc is not None:
                try:
                    await nc.close()
                except Exception:
                    logger.debug("closing the probe connection failed", exc_info=True)

    try:
        return await asyncio.wait_for(_probe(), timeout=PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        return QueueHealth(reachable=False, detail=f"the queue did not answer within {PROBE_TIMEOUT_SECONDS}s")
    except Exception as exc:
        return QueueHealth(reachable=False, detail=f"{type(exc).__name__}: {exc}")
