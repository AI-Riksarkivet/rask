"""A service that is merely SLOW must not be reported as absent ([[XC-039]]).

IT LIVES HERE, NOT BESIDE THE HELPER. `tests/e2e-py` is the LIVE suite directory, and
`test_e2e_collection_gate` refuses a module there that no marker or make target selects — correctly,
since a file that collects, deselects and never runs is the "reports green, covers nothing" failure in
miniature. This needs no live estate at all: the probe, the clock and the sleep are injected, so it is
an ordinary unit test and belongs in the ordinary suite, where a regression in this policy is caught
in review rather than by a live run nobody has taken recently.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# The helper is e2e INFRASTRUCTURE and lives with the suites that use it; `tests/e2e-py` is a flat
# helper directory its own conftest puts on `sys.path`, which does not apply from here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e-py"))

from liveness import wait_until_live  # noqa: E402 — must follow the path insert above


class _Clock:
    """A monotonic clock advanced only by the sleeps the helper asks for, so a 60 s budget costs none."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def _probe_failing(times: int):  # noqa: ANN202 — a closure factory, typed by its use below
    """A probe that raises for the first `times` attempts, then succeeds."""
    state = {"n": 0}

    def probe(url: str, timeout: float) -> None:
        state["n"] += 1
        if state["n"] <= times:
            raise ConnectionError("connection refused")

    return probe, state


def test_a_service_that_answers_immediately_is_live_without_sleeping() -> None:
    clock = _Clock()

    live, waited, error = wait_until_live("http://x/livez", probe=lambda _u, _t: None, now=clock.now, sleep=clock.sleep)

    assert (live, waited, error) == (True, 0.0, "")
    assert clock.slept == [], "a healthy service must cost no wait at all"


def test_a_SLOW_service_is_waited_for_rather_than_called_absent() -> None:
    """THE DEFECT: five seconds and one attempt turned a loaded-but-healthy producer into a skip."""
    clock = _Clock()
    probe, state = _probe_failing(4)

    live, waited, error = wait_until_live("http://x/livez", probe=probe, now=clock.now, sleep=clock.sleep)

    assert live is True, f"gave up on a service that came up after {state['n']} attempts"
    assert waited > 0 and error == ""
    assert state["n"] == 5


def test_an_ABSENT_service_is_reported_with_how_long_it_waited() -> None:
    """The other half. "not reachable" that does not say how long it tried is what made this invisible."""
    clock = _Clock()
    probe, _ = _probe_failing(10_000)

    live, waited, error = wait_until_live("http://x/livez", probe=probe, budget_seconds=30.0, now=clock.now, sleep=clock.sleep)

    assert live is False
    assert waited >= 30.0, f"gave up after {waited}s against a 30s budget"
    assert "ConnectionError" in error, error


def test_ONE_attempt_is_made_even_with_no_budget() -> None:
    """A zero budget must still ask. Checking the clock BEFORE the attempt would report a service
    absent having never probed it — a wrong answer delivered instantly."""
    clock = _Clock()
    probe, state = _probe_failing(0)

    live, _waited, _error = wait_until_live("http://x/livez", probe=probe, budget_seconds=0.0, now=clock.now, sleep=clock.sleep)

    assert live is True and state["n"] == 1


def test_the_per_attempt_TIMEOUT_reaches_the_probe() -> None:
    """A generous budget spent on 5 s attempts is still 5 s attempts — the timeout is the other half of
    the fix, and nothing else here would notice it being dropped."""
    seen: list[float] = []

    def probe(url: str, timeout: float) -> None:
        seen.append(timeout)

    wait_until_live("http://x/livez", probe=probe, attempt_timeout_seconds=15.0)

    assert seen == [15.0]


@pytest.mark.parametrize("budget", [1.0, 5.0, 30.0])
def test_it_never_sleeps_past_its_budget(budget: float) -> None:
    """A helper that overshot would make an absent target cost more than the suite budgeted for it."""
    clock = _Clock()
    probe, _ = _probe_failing(10_000)

    _live, waited, _error = wait_until_live("http://x/livez", probe=probe, budget_seconds=budget, interval_seconds=3.0, now=clock.now, sleep=clock.sleep)

    assert waited < budget + 3.0, f"waited {waited}s against a {budget}s budget"
