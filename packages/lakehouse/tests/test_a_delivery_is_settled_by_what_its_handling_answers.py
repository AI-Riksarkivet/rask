"""A delivery is settled by what its handling answers: acknowledged, asked for again after the lane's delay, or parked.

[[XC-199]] (#787). A pull consumer has no dead letter: the bus delivers a message until it is acknowledged or ended, so
the rule that ends one is the lakehouse's own (`lakehouse.bus_delivery.judged`). Here is that rule alone, each branch
once, with a handler and a park of the test's own. What a real broker does with the answer (a delayed redelivery, an
ended message that never comes back) is the nats-auth lane's to show
(`tests/e2e-py/test_nats_admits_each_client_only_to_its_own_subjects.py`).
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from lakehouse.bus_delivery import MAX_ENVELOPE_DEPTH, Delivery, Handle, Lane, Outcome, Settlement, Verdict, judged


#: A lane that handles a message on three deliveries, two minutes apart.
LANE = Lane(stream="LINEAGE", consumer="lineage-ingest", attempts=3, retry_seconds=120.0, ack_wait_seconds=300.0)


class _Park:
    """Keeps what it was asked to park, and why; ``failing`` is a park its store does not take."""

    def __init__(self, *, failing: BaseException | None = None) -> None:
        self.kept: list[tuple[int, str]] = []
        self._failing = failing

    async def __call__(self, delivery: Delivery, reason: str) -> None:
        if self._failing is not None:
            raise self._failing
        self.kept.append((delivery.sequence, reason))


def _delivery(attempt: int) -> Delivery:
    return Delivery(subject="lineage.events.v1", body=b'{"data": {}}', sequence=41, attempt=attempt)


def _answering(verdict: Verdict) -> tuple[list[int], Handle]:
    """A handler that answers ``verdict``, and the attempts it was handed."""
    handled: list[int] = []

    async def handle(delivery: Delivery) -> Verdict:
        handled.append(delivery.attempt)
        return verdict

    return handled, handle


@pytest.mark.parametrize(
    ("verdict", "attempt", "settled", "parked"),
    [
        pytest.param(Verdict.TAKEN, 1, Settlement(outcome=Outcome.TAKEN), [], id="taken-is-acknowledged"),
        pytest.param(Verdict.AGAIN, 2, Settlement(outcome=Outcome.AGAIN, delay_seconds=120.0), [], id="again-before-the-last-delivery-waits-the-lanes-delay"),
        pytest.param(
            Verdict.AGAIN,
            3,
            Settlement(outcome=Outcome.PARKED),
            [(41, "asked for again on its last delivery (3 of 3)")],
            id="again-on-the-last-delivery-is-parked",
        ),
        pytest.param(Verdict.PARK, 1, Settlement(outcome=Outcome.PARKED), [(41, "its handling asked for it to be parked")], id="park-is-parked-at-once"),
    ],
)
@pytest.mark.asyncio
async def test_a_delivery_is_settled_by_its_handlers_verdict(verdict: Verdict, attempt: int, settled: Settlement, parked: list[tuple[int, str]]) -> None:
    handled, handle = _answering(verdict)
    park = _Park()

    answer = await judged(_delivery(attempt), LANE, handle, park)

    assert (answer, park.kept, handled) == (settled, parked, [attempt])


@pytest.mark.asyncio
async def test_a_delivery_past_the_lanes_last_is_parked_without_being_handled() -> None:
    handled, handle = _answering(Verdict.TAKEN)
    park = _Park()

    answer = await judged(_delivery(4), LANE, handle, park)

    assert (answer, park.kept, handled) == (Settlement(outcome=Outcome.PARKED), [(41, "every one of its 3 deliveries ended before its handling did")], [])


@pytest.mark.parametrize(
    ("attempt", "said", "settled", "parked"),
    [
        pytest.param(1, "the catalog is away", Settlement(outcome=Outcome.AGAIN, delay_seconds=120.0), [], id="before-the-last-delivery"),
        pytest.param(
            3,
            "the catalog is away",
            Settlement(outcome=Outcome.PARKED),
            [(41, "its last delivery (3 of 3) failed: ConnectionError: the catalog is away")],
            id="on-the-last-delivery",
        ),
        pytest.param(
            3,
            "no table 'pages\ud800'",
            Settlement(outcome=Outcome.PARKED),
            [(41, "its last delivery (3 of 3) failed: ConnectionError: no table 'pages\\ud800'")],
            id="on-the-last-delivery-quoting-text-no-store-can-encode",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_handling_that_fails_counts_as_asked_for_again(
    caplog: pytest.LogCaptureFixture, attempt: int, said: str, settled: Settlement, parked: list[tuple[int, str]]
) -> None:
    # The seam logs a failure with its traceback as the handler raised it, and pytest-xdist cannot carry a report whose
    # captured log holds text with no UTF-8 form (measured on pytest-xdist as locked: the worker is lost).
    caplog.set_level(logging.ERROR, logger="lakehouse.bus_delivery")

    async def handle(_: Delivery) -> Verdict:
        raise ConnectionError(said)

    park = _Park()

    answer = await judged(_delivery(attempt), LANE, handle, park)

    assert (answer, park.kept) == (settled, parked)


@pytest.mark.asyncio
async def test_a_handling_that_outruns_the_lanes_ack_wait_is_cut_and_asked_for_again() -> None:
    # The bound is the ack wait less the time kept for the settle itself, so this lane leaves a handling 50 ms.
    lane = LANE.model_copy(update={"ack_wait_seconds": Lane.SETTLE_SECONDS + 0.05})
    cut = asyncio.Event()

    async def handle(_: Delivery) -> Verdict:
        try:
            await asyncio.Event().wait()
        finally:
            cut.set()
        return Verdict.TAKEN

    # Cut inside the time kept for the settle, so the bus has not handed the message to another replica meanwhile.
    async with asyncio.timeout(Lane.SETTLE_SECONDS / 2):
        answer = await judged(_delivery(1), lane, handle, _Park())

    assert (answer, cut.is_set()) == (Settlement(outcome=Outcome.AGAIN, delay_seconds=120.0), True)


@pytest.mark.asyncio
async def test_a_park_has_only_what_the_handling_left_of_the_lanes_bound() -> None:
    """Past the ack wait the bus hands the message to another replica, which would park it beside this one: a park that
    begins once the handling has used the bound up is cut, and the delivery comes again to be parked then."""
    lane = LANE.model_copy(update={"ack_wait_seconds": Lane.SETTLE_SECONDS + 0.05})
    kept: list[int] = []

    async def handle(_: Delivery) -> Verdict:
        await asyncio.Event().wait()
        return Verdict.TAKEN

    async def park(delivery: Delivery, _: str) -> None:
        # A park asks its store, and so yields: none of the bound is left for even that.
        await asyncio.sleep(0)
        kept.append(delivery.sequence)

    async with asyncio.timeout(Lane.SETTLE_SECONDS / 2):
        answer = await judged(_delivery(3), lane, handle, park)

    assert (answer, kept) == (Settlement(outcome=Outcome.AGAIN, delay_seconds=120.0), [])


def test_a_lane_whose_ack_wait_leaves_no_time_to_handle_a_delivery_is_refused() -> None:
    """Every handling on such a lane would be cut as it began, and every message parked unhandled."""
    with pytest.raises(ValueError, match="leaves no time to handle a delivery"):
        LANE.model_validate({**LANE.model_dump(), "ack_wait_seconds": Lane.SETTLE_SECONDS})


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ConnectionError("the catalog is away"), id="its-store-is-away"),
        pytest.param(asyncio.CancelledError(), id="what-it-awaited-was-cancelled-beneath-it"),
    ],
)
@pytest.mark.asyncio
async def test_a_park_that_fails_leaves_the_delivery_to_come_again(failure: BaseException) -> None:
    _, handle = _answering(Verdict.PARK)

    answer = await judged(_delivery(3), LANE, handle, _Park(failing=failure))

    assert answer == Settlement(outcome=Outcome.AGAIN, delay_seconds=120.0)


@pytest.mark.asyncio
async def test_a_cancellation_raised_inside_a_handling_is_the_deliverys_failure() -> None:
    """A future the handling awaited was cancelled beneath it, and nobody asked the task that judges the delivery to
    stop: passed on, the cancellation would end the loop behind it as a stop somebody asked for."""

    async def handle(_: Delivery) -> Verdict:
        raise asyncio.CancelledError

    answer = await judged(_delivery(1), LANE, handle, _Park())

    assert answer == Settlement(outcome=Outcome.AGAIN, delay_seconds=120.0)


@pytest.mark.asyncio
async def test_a_handling_cancelled_as_the_process_stops_is_settled_by_nobody() -> None:
    handling = asyncio.Event()

    async def handle(_: Delivery) -> Verdict:
        handling.set()
        await asyncio.Event().wait()
        return Verdict.TAKEN

    park = _Park()
    judging = asyncio.create_task(judged(_delivery(3), LANE, handle, park))
    async with asyncio.timeout(Lane.SETTLE_SECONDS / 2):
        await handling.wait()

    judging.cancel()

    with pytest.raises(asyncio.CancelledError):
        await judging
    assert park.kept == []


def _nested(levels: int) -> bytes:
    """An object nested ``levels`` deep, the outermost one included."""
    return b'{"in":' * (levels - 1) + b"{}" + b"}" * (levels - 1)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json", id="no-json"),
        pytest.param(b'["a", "list"]', id="json-that-is-no-object"),
        pytest.param(b"[" * 20_000, id="nested-deeper-than-the-parser-reads"),
        pytest.param(_nested(MAX_ENVELOPE_DEPTH + 1), id="nested-one-level-deeper-than-an-envelope-is-read"),
    ],
)
def test_a_body_that_is_no_json_object_carries_no_envelope(body: bytes) -> None:
    """Whatever a publisher sent, reading it must answer: a body the parser gives up on would otherwise end the loop
    that took it, before any rule could settle the message, and one a later reader gives up on would fail whatever
    reads it next."""
    assert Delivery(subject="lineage.events.v1", body=body, sequence=41, attempt=1).envelope() is None


def test_a_body_nested_as_deep_as_an_envelope_is_read_carries_its_envelope() -> None:
    body = _nested(MAX_ENVELOPE_DEPTH)

    assert Delivery(subject="lineage.events.v1", body=body, sequence=41, attempt=1).envelope() == json.loads(body)
