"""A fixed timer's work runs once per window however many replicas run its loop ([[XC-199]]).

Each replica's loop claims the window on the control root with one conditional write, and the store lets one land. Two
:class:`lakehouse.timers.Timer` on one root stand for two replicas of a service; the root is a local directory, where the
conditional write is the operating system's (`lakehouse.storage.conditional_records`). A test waits for a window exactly as long as the
timer's own turn says the next one is away.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, NoReturn

import pytest
from botocore.exceptions import ClientError
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader, NumberDataPoint

from lakehouse import timers
from lakehouse.storage import conditional_records
from lakehouse.storage.errors import store_call
from lakehouse.storage.options import StorageOptions
from lakehouse.timers import Outcome, Timer, Turn, claim_key


PERIOD = 0.3


def _replica(root: Path, ran: list[str], *, name: str = "relay", holder: str, period: float = PERIOD, held: bool = False, fails: bool = False) -> Timer:
    async def work() -> None:
        ran.append(holder)
        if fails:
            raise OSError("the store did not answer")

    return Timer(name, period=period, work=work, root=str(root), storage_options=StorageOptions, held=lambda: held, holder=holder)


async def _next_window(timer: Timer, after: Turn) -> Turn:
    """The timer's first turn in the window after ``after``'s: the loop's own waiting, bounded.

    A wait is measured on the monotonic clock and a window on the wall clock, so a turn taken exactly when the wait ends
    can still find the old window; the loop then waits what that turn says, as this does.
    """
    deadline = monotonic() + 10
    turn = after
    while monotonic() < deadline:
        await asyncio.sleep(turn.wait)
        turn = await timer.turn()
        if turn.window != after.window:
            return turn
    raise AssertionError(f"no new window within the deadline: {turn}")


@pytest.mark.asyncio
async def test_two_replicas_of_one_timer_run_each_window_once(tmp_path: Path) -> None:
    ran: list[str] = []
    one, two = _replica(tmp_path, ran, holder="pod-1"), _replica(tmp_path, ran, holder="pod-2")
    runs_of: Counter[str] = Counter()
    for _ in range(4):
        turns = await asyncio.gather(one.turn(), two.turn(), one.turn(), two.turn())
        for turn in turns:
            if turn.outcome is Outcome.RAN:
                assert turn.window is not None
                runs_of[turn.window.isoformat()] += 1
        await asyncio.sleep(max(turn.wait for turn in turns))

    assert len(runs_of) >= 3, "the windows went by unclaimed"
    assert set(runs_of.values()) == {1}, f"a window's work ran more than once: {runs_of}"
    assert len(ran) == len(runs_of)


@pytest.mark.asyncio
async def test_a_replica_that_finds_the_window_claimed_drops_what_only_the_claimer_knows(tmp_path: Path) -> None:
    ran: list[str] = []
    dropped: list[str] = []

    async def work() -> None:
        ran.append("pod-2")

    one = _replica(tmp_path, ran, holder="pod-1", period=600)
    two = Timer("relay", period=600, work=work, root=str(tmp_path), storage_options=StorageOptions, elsewhere=lambda: dropped.append("pod-2"), holder="pod-2")

    first = await one.turn()
    second = await two.turn()

    assert (first.outcome, second.outcome) == (Outcome.RAN, Outcome.ELSEWHERE)
    assert (ran, dropped) == (["pod-1"], ["pod-2"])
    assert 0 < second.wait <= 600


def _forbidden(*_: object) -> NoReturn:
    """What the read raises when S3 answers a key to an identity that may not list its bucket, whether or not it exists."""
    answer: Any = {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403, "HTTPHeaders": {}}}
    with store_call("reading the claim"):
        raise ClientError(answer, "GetObject")


@pytest.mark.asyncio
async def test_a_store_that_answers_a_missing_claim_as_forbidden_still_takes_the_first_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(conditional_records, "read_json_at", _forbidden)

    first = await _replica(tmp_path, ran, holder="pod-1", period=600).turn()
    second = await _replica(tmp_path, ran, holder="pod-2", period=600).turn()

    monkeypatch.undo()
    stored = conditional_records.read_json(str(tmp_path), StorageOptions(), claim_key("relay"))
    assert (first.outcome, second.outcome) == (Outcome.RAN, Outcome.UNREAD), "a claim that exists and cannot be read is a fault, never another replica's window"
    assert ran == ["pod-1"]
    assert stored is not None
    assert stored[0]["by"] == "pod-1"


@pytest.mark.asyncio
async def test_a_claim_of_the_next_window_by_a_store_clock_a_little_ahead_is_left_alone(tmp_path: Path) -> None:
    """A store's front ends may differ by a second at a boundary: the replica reading the one behind finds the next
    window claimed, and running over that claim would run two windows twice."""
    ran: list[str] = []
    period = 600
    following = datetime.fromtimestamp((datetime.now(UTC).timestamp() // period + 1) * period, UTC).isoformat()
    claim = {"timer": "relay", "window": following, "claimed_at": following, "by": "pod-2"}
    conditional_records.create_json(str(tmp_path), StorageOptions(), claim_key("relay"), record=claim)

    turn = await _replica(tmp_path, ran, holder="pod-1", period=period).turn()

    stored = conditional_records.read_json(str(tmp_path), StorageOptions(), claim_key("relay"))
    assert turn.outcome is Outcome.ELSEWHERE
    assert ran == []
    assert stored is not None
    assert stored[0] == claim


@pytest.mark.asyncio
async def test_a_claim_dated_more_than_a_period_after_the_stores_own_time_is_claimed_over(tmp_path: Path) -> None:
    ran: list[str] = []
    ahead = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    conditional_records.create_json(
        str(tmp_path),
        StorageOptions(),
        claim_key("relay"),
        record={"timer": "relay", "window": ahead, "claimed_at": ahead, "by": "a-clock-ahead"},
    )

    turn = await _replica(tmp_path, ran, holder="pod-1", period=600).turn()

    assert turn.outcome is Outcome.RAN
    assert ran == ["pod-1"]


@pytest.mark.asyncio
async def test_timers_of_different_names_claim_apart(tmp_path: Path) -> None:
    ran: list[str] = []
    relay, expiry = _replica(tmp_path, ran, holder="relay", period=600), _replica(tmp_path, ran, name="expiry", holder="expiry", period=600)

    outcomes = [(await relay.turn()).outcome, (await expiry.turn()).outcome]

    assert outcomes == [Outcome.RAN, Outcome.RAN]
    assert sorted(ran) == ["expiry", "relay"]


@pytest.mark.asyncio
async def test_a_held_timer_claims_nothing_and_runs_nothing(tmp_path: Path) -> None:
    ran: list[str] = []

    turn = await _replica(tmp_path, ran, holder="pod-1", held=True).turn()

    assert turn.outcome is Outcome.HELD
    assert ran == []
    assert conditional_records.read_json(str(tmp_path), StorageOptions(), claim_key("relay")) is None


@pytest.mark.asyncio
async def test_work_that_fails_keeps_the_timer_and_runs_again_next_window(tmp_path: Path) -> None:
    ran: list[str] = []
    timer = _replica(tmp_path, ran, holder="pod-1", fails=True)

    failed = await timer.turn()
    again = await _next_window(timer, failed)

    assert (failed.outcome, again.outcome) == (Outcome.FAILED, Outcome.FAILED)
    assert ran == ["pod-1", "pod-1"]


@pytest.mark.asyncio
async def test_each_run_of_a_timers_work_is_timed_by_timer_and_by_how_it_ended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn that ran no work (another replica's window) records no time: the histogram's count is the work's runs."""
    reader = InMemoryMetricReader()
    histogram = MeterProvider(metric_readers=[reader]).get_meter("lance.timers").create_histogram("timer.work.duration", unit="s")
    monkeypatch.setattr(timers, "_work_duration", histogram)
    ran: list[str] = []

    outcomes = [
        (await _replica(tmp_path, ran, name="relay", holder="pod-1").turn()).outcome,
        (await _replica(tmp_path, ran, name="relay", holder="pod-2").turn()).outcome,
        (await _replica(tmp_path, ran, name="reclaim", holder="pod-1", fails=True).turn()).outcome,
    ]

    assert outcomes == [Outcome.RAN, Outcome.ELSEWHERE, Outcome.FAILED]
    data = reader.get_metrics_data()
    assert data is not None
    timed = {
        (str((point.attributes or {})["timer"]), str((point.attributes or {})["outcome"])): (point.count, point.sum >= 0)
        for point in data.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points
        if isinstance(point, HistogramDataPoint)
    }
    assert timed == {("relay", "ran"): (1, True), ("reclaim", "failed"): (1, True)}


@pytest.mark.asyncio
async def test_a_period_changed_in_git_is_due_at_its_own_next_window(tmp_path: Path) -> None:
    """The claim names its window by when it began, so a claim made under another period is judged on the store's clock,
    never on a count of windows of another length."""
    ran: list[str] = []
    short = await _replica(tmp_path, ran, holder="pod-1", period=0.2).turn()
    assert short.outcome is Outcome.RAN

    turn = await _next_window(_replica(tmp_path, ran, holder="pod-1", period=0.4), short)

    assert turn.outcome is Outcome.RAN


@pytest.mark.asyncio
async def test_a_claim_that_cannot_be_read_runs_nothing_and_is_read_again_within_the_period(tmp_path: Path) -> None:
    ran: list[str] = []
    (tmp_path / claim_key("relay")).parent.mkdir(parents=True)
    (tmp_path / claim_key("relay")).write_text("not json")

    turn = await _replica(tmp_path, ran, holder="pod-1", period=600).turn()

    assert turn.outcome is Outcome.UNREAD
    assert ran == []
    assert 0 < turn.wait < 600


@pytest.mark.parametrize(
    "record",
    [
        pytest.param('{"timer": "relay", "window": "2026-01-01T00:00:00Z"}', id="fields-missing"),
        pytest.param('{"timer": "relay", "window": "2999-01-01T00:00:00", "claimed_at": "2999-01-01T00:00:00", "by": "pod-0"}', id="a-window-on-no-clock"),
    ],
)
@pytest.mark.asyncio
async def test_a_record_at_the_timers_key_that_is_no_claim_is_claimed_over(tmp_path: Path, record: str) -> None:
    """The claim is the timer's own bookkeeping, so a record that is none says nobody holds the window: the conditional
    replace still lets one replica win, where a refusal would stop the timer on every replica until a hand edit."""
    ran: list[str] = []
    (tmp_path / claim_key("relay")).parent.mkdir(parents=True)
    (tmp_path / claim_key("relay")).write_text(record)
    one, two = _replica(tmp_path, ran, holder="pod-1", period=600), _replica(tmp_path, ran, holder="pod-2", period=600)

    first, second = await one.turn(), await two.turn()

    stored = conditional_records.read_json(str(tmp_path), StorageOptions(), claim_key("relay"))
    assert (first.outcome, second.outcome, ran) == (Outcome.RAN, Outcome.ELSEWHERE, ["pod-1"])
    assert stored is not None
    assert stored[0]["by"] == "pod-1"


@pytest.mark.asyncio
async def test_a_replica_that_turns_again_in_the_window_it_ran_keeps_what_its_tick_measured(tmp_path: Path) -> None:
    """A wait is measured on the monotonic clock and a window on the store's, so the replica that ran a window can turn
    again inside it. It runs nothing, and it is not told the window was another replica's: what its tick measured is
    still the only reading of the window."""
    ran: list[str] = []
    dropped: list[str] = []

    async def work() -> None:
        ran.append("pod-1")

    one = Timer("relay", period=600, work=work, root=str(tmp_path), storage_options=StorageOptions, elsewhere=lambda: dropped.append("pod-1"), holder="pod-1")
    assert (await one.turn()).outcome is Outcome.RAN

    again = await one.turn()

    assert (again.outcome, ran, dropped) == (Outcome.ELSEWHERE, ["pod-1"], [])


@pytest.mark.asyncio
async def test_the_loop_takes_turn_after_turn_until_it_is_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every outcome's series of the turn counter exists from the loop's start, the ones that never happen at 0: an
    alert reads a counter's increase, and a series born at a timer's first failed turn would show none."""
    reader = InMemoryMetricReader()
    monkeypatch.setattr(timers, "_turns", MeterProvider(metric_readers=[reader]).get_meter("lance.timers").create_counter("timer.turns"))
    ran: list[str] = []
    loop = asyncio.create_task(_replica(tmp_path, ran, holder="pod-1", period=0.1).run())

    deadline = monotonic() + 5
    while len(ran) < 3 and monotonic() < deadline:
        await asyncio.sleep(0.02)
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)

    assert len(ran) >= 3
    assert loop.cancelled()
    data = reader.get_metrics_data()
    assert data is not None
    turns = {
        str((point.attributes or {})["outcome"]): int(point.value)
        for point in data.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points
        if isinstance(point, NumberDataPoint)
    }
    # A turn taken as its wait ends can still find the window this replica ran, which counts as claimed before the
    # turn: `elsewhere` is there from the start and may have risen.
    assert set(turns) == {"ran", "failed", "elsewhere", "held", "unread"}
    assert {outcome: turns[outcome] for outcome in ("failed", "held", "unread")} == {"failed": 0, "held": 0, "unread": 0}
    assert turns["ran"] >= 3
