"""What a consumer does with one delivery from the bus: acknowledge it, ask for it again, or park it ([[XC-199]]).

A PULL CONSUMER HAS NO DEAD LETTER. JetStream delivers a message until it is acknowledged or ended, and a message asked
for again with no delay comes back at once (measured on nats-server 2.15.2, 2026-11-13: 0.00 s). So the rule that ends
a delivery taken through `lakehouse.events.bus.Bus.consume` is the lakehouse's own, stated here, and each lane names its
numbers (:class:`Lane`):

* a handling answers :class:`Verdict`: taken, again, or park;
* *again* waits the lane's delay, every time, and on the lane's last delivery the message is parked instead;
* *park* is the lane's own keeping of a message nothing more will be done with by itself (``park``), after which the
  message is ended on the bus. A park that fails is asked for again: it is the last word on a delivery, so a store that
  did not take it must not be the reason the message is gone;
* a delivery past the lane's last was never handled to its end (each earlier one outlived its process or its ack wait),
  so it is parked unhandled: a message that ends its handler every time ends no more than ``attempts`` of them;
* a delivery's handling and its park share one bound, the consumer's ack wait less what is kept for the settle: past
  the ack wait the bus hands the message to another replica, whatever this one is still doing with it.

This module names no broker: `lakehouse.events.bus` applies a :class:`Settlement` to the message it came for.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, ClassVar, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


log = logging.getLogger(__name__)

#: Bounds what a failure's own text can put into a parked delivery's reason and a log line.
_MAX_FAILURE_CHARS = 300

#: How deep a body nests and is still read as an envelope: the envelope is the first level, and each object or array
#: inside another is one more. A delivery is its publisher's bytes, and everything that reads one walks it: a model, a
#: serialiser, the row a park keeps and every later read of that row, a log handler. Each walks by recursion within a
#: bound of its own, and a value just inside one reader's bound is past the next reader's. Measured 2026-11-14 on
#: CPython 3.13.15 and the locked libraries: in a task `json.loads` reads an envelope whose id is 9,993 lists deep;
#: `str` of that id fails two levels sooner; opentelemetry-sdk's log handler renders a value 491 lists deep; and
#: lineage's event model accepts a facet 254 lists deep, an envelope 258 deep, pydantic refusing to serialise one
#: deeper. 300 refuses nothing a door of the lakehouse accepts, and hands on nothing a reader of it cannot walk.
MAX_ENVELOPE_DEPTH: Final = 300


class Verdict(StrEnum):
    """What a handling answers about the delivery it was handed."""

    #: Handled, or refused for good: nothing more is to be done with it.
    TAKEN = "taken"
    #: Not now: a dependency did not answer, and the same delivery may be taken later.
    AGAIN = "again"
    #: No redelivery changes the answer, and a repair might: kept for one, and ended on the bus.
    PARK = "park"


class Outcome(StrEnum):
    """How a delivery was settled, as the bus is told and a lane counts it."""

    TAKEN = "taken"
    AGAIN = "again"
    PARKED = "parked"


class Lane(BaseModel):
    """One durable consumer a service takes its deliveries from, and the numbers its deliveries are settled by."""

    model_config = ConfigDict(frozen=True)

    #: What a lane keeps of its consumer's ack wait for the settle itself: a delivery's handling, and its park after it,
    #: are cut this long before the bus would hand its message to another replica beneath them.
    SETTLE_SECONDS: ClassVar[float] = 10.0

    stream: str
    #: The chart's durable consumer on ``stream``: a service binds to it and creates nothing.
    consumer: str
    #: How many deliveries a message is handled on before it is parked.
    attempts: int = Field(ge=1)
    #: How long a delivery asked for again waits before the bus delivers it again.
    retry_seconds: float = Field(ge=0)
    #: The consumer's ack wait, as the chart declares it.
    ack_wait_seconds: float
    #: How many deliveries are handled at once.
    in_flight: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _an_ack_wait_leaves_time_to_handle(self) -> Self:
        if self.ack_wait_seconds <= self.SETTLE_SECONDS:
            raise ValueError(f"an ack wait of {self.ack_wait_seconds} s leaves no time to handle a delivery ({self.SETTLE_SECONDS} s is kept for its settle)")
        return self

    @property
    def handling_seconds(self) -> float:
        """How long one delivery's handling and its park may take together before they are cut."""
        return self.ack_wait_seconds - self.SETTLE_SECONDS


class Delivery(BaseModel):
    """One message a consumer was handed."""

    model_config = ConfigDict(frozen=True)

    subject: str
    body: bytes
    #: Where the stream keeps the message.
    sequence: int
    #: How many times the bus has delivered it, this one included.
    attempt: int

    def envelope(self) -> dict[str, Any] | None:
        """The envelope the message carries, or None for a body that is none (:func:`envelope_of`)."""
        return envelope_of(self.body)


def envelope_of(body: bytes) -> dict[str, Any] | None:
    """The envelope a message's ``body`` carries, or None for a body that is none: bytes that are no JSON, a JSON value
    that is no object, or one nested deeper than `MAX_ENVELOPE_DEPTH` (past the parser's own bound that is a
    `RecursionError`, which is no `ValueError`).

    EVERY READER OF A MESSAGE'S BODY READS IT HERE: each lane's deliveries, and a publisher reading back what the stream
    kept under its event's id, which another publisher may have put there.
    """
    try:
        parsed = json.loads(body)
    except ValueError, RecursionError:
        return None
    return parsed if isinstance(parsed, dict) and _nests_within(parsed, MAX_ENVELOPE_DEPTH) else None


def _nests_within(value: object, levels: int) -> bool:
    """Whether ``value`` nests objects and arrays no deeper than ``levels``, walked a level at a time: a walk by
    recursion would have the bound this one enforces."""
    holders = [value] if isinstance(value, (dict, list)) else []
    for _ in range(levels):
        if not holders:
            return True
        holders = [held for holder in holders for held in (holder.values() if isinstance(holder, dict) else holder) if isinstance(held, (dict, list))]
    return not holders


class Settlement(BaseModel):
    """What the bus is told about a delivery."""

    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    #: How long the bus waits before it delivers the message again; 0 unless it is asked for again.
    delay_seconds: float = 0.0


type Handle = Callable[[Delivery], Awaitable[Verdict]]
#: Keeps a delivery nothing more will be done with by itself, with why; raises when its store did not take it.
type Park = Callable[[Delivery, str], Awaitable[None]]


class _Answer(BaseModel):
    """What one handling answered, and how it failed when it did."""

    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    failure: str = ""


async def judged(delivery: Delivery, lane: Lane, handle: Handle, *, park: Park) -> Settlement:
    """Handle ``delivery`` and answer what the bus is to be told about it.

    Args:
        delivery: The message, with how many times the bus has delivered it.
        lane: The numbers this consumer's deliveries are settled by.
        handle: The service's handling. An exception it raises, and a handling cut at the lane's bound, count as
            :attr:`Verdict.AGAIN`.
        park: The lane's keeping of a delivery that is ended unhandled or refused. It has what the handling left of
            the lane's bound, and one cut there is a park that failed.

    Returns:
        The settlement: taken, again with the lane's delay, or parked once ``park`` has kept the delivery.
    """
    until = asyncio.get_running_loop().time() + lane.handling_seconds
    if delivery.attempt > lane.attempts:
        return await _parked(delivery, lane, park, reason=f"every one of its {lane.attempts} deliveries ended before its handling did", until=until)
    answer = await _handled(delivery, lane, handle, until=until)
    if answer.verdict is Verdict.TAKEN:
        return Settlement(outcome=Outcome.TAKEN)
    if answer.verdict is Verdict.PARK:
        return await _parked(delivery, lane, park, reason="its handling asked for it to be parked", until=until)
    if delivery.attempt < lane.attempts:
        return _again(lane)
    last = f"{delivery.attempt} of {lane.attempts}"
    reason = f"its last delivery ({last}) failed: {answer.failure}" if answer.failure else f"asked for again on its last delivery ({last})"
    return await _parked(delivery, lane, park, reason=reason, until=until)


def _again(lane: Lane) -> Settlement:
    return Settlement(outcome=Outcome.AGAIN, delay_seconds=lane.retry_seconds)


def being_cancelled() -> bool:
    """Whether the task running this was asked to stop.

    A `CancelledError` raised while it was not comes from what the task awaited (a future another task cancelled), so it
    is that work's failure. Passed on, it would end the loop the task serves as a cancelled task, which a supervisor
    reads as a stop it asked for: the lane would be gone with its process still Ready.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def _handled(delivery: Delivery, lane: Lane, handle: Handle, *, until: float) -> _Answer:
    """What ``handle`` answers about ``delivery`` before the loop's clock reads ``until``, the lane's bound; a failure
    or a cut handling is *again*."""
    bound = asyncio.timeout_at(until)
    try:
        async with bound:
            return _Answer(verdict=await handle(delivery))
    except (Exception, asyncio.CancelledError) as exc:
        # Every failure of a handling is the delivery's, asked for again or parked by the rule above: this is the
        # consumer's outermost frame, and a handler that raised must not end the loop every other delivery waits on.
        # A request to stop, and a native library's panic, pass.
        if isinstance(exc, asyncio.CancelledError) and being_cancelled():
            raise
        failure = f"TimeoutError: its handling outran {lane.handling_seconds:.0f} s" if bound.expired() else _said_by(exc)
        log.warning("delivery_handling_failed", exc_info=exc, extra={**_named(delivery, lane), "error": failure})
        return _Answer(verdict=Verdict.AGAIN, failure=failure)


def _said_by(exc: BaseException) -> str:
    """What a failure says of itself, bounded and in text any store keeps. A failure may quote what the handling was
    handed, and a lone surrogate there has no UTF-8 form: a park handed it in its reason would be refused by its own
    store, on every delivery after."""
    return f"{type(exc).__name__}: {exc}"[:_MAX_FAILURE_CHARS].encode("utf-8", "backslashreplace").decode()


async def _parked(delivery: Delivery, lane: Lane, park: Park, *, reason: str, until: float) -> Settlement:
    """Park ``delivery`` before the loop's clock reads ``until`` and answer that it is ended; a park that fails, or is
    cut there, answers that it is asked for again."""
    try:
        async with asyncio.timeout_at(until):
            await park(delivery, reason)
    except (Exception, asyncio.CancelledError) as exc:
        # The park is the last word on a delivery, so no failure of it may be: the delivery comes again, and is parked
        # then, with the lane's whole bound when it comes past its last.
        if isinstance(exc, asyncio.CancelledError) and being_cancelled():
            raise
        log.error("delivery_not_parked", exc_info=exc, extra={**_named(delivery, lane), "reason": reason})
        return _again(lane)
    log.warning("delivery_parked", extra={**_named(delivery, lane), "reason": reason})
    return Settlement(outcome=Outcome.PARKED)


def _named(delivery: Delivery, lane: Lane) -> dict[str, object]:
    return {"stream": lane.stream, "consumer": lane.consumer, "sequence": delivery.sequence, "attempt": delivery.attempt}


__all__ = [
    "MAX_ENVELOPE_DEPTH",
    "Delivery",
    "Handle",
    "Lane",
    "Outcome",
    "Park",
    "Settlement",
    "Verdict",
    "being_cancelled",
    "envelope_of",
    "judged",
]
