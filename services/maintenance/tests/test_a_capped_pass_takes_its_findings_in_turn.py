"""What a capped pass takes in one window of the audit's schedule (`maintenance.services.drain.share_of`): the next share
of an order no run chooses, so a set that stays as it is is taken whole in as many windows as it has shares."""

import math

from maintenance.services import drain


#: The sweep's measured backlog and its default cap (989 orphaned trash records, 50 a run).
FINDINGS = [f"record-{n}" for n in range(989)]
CAP = 50
#: An hourly window of late 2026, where the position has wrapped many times over.
WINDOW = 496_000


def _taken(findings: list[str], windows: range) -> set[str]:
    return {name for window in windows for name in drain.share_of(findings, cap=CAP, window=window, name=str)[0]}


def test_a_set_that_stays_as_it_is_is_taken_whole_in_as_many_windows_as_it_has_shares() -> None:
    turn = math.ceil(len(FINDINGS) / CAP)

    assert _taken(FINDINGS, range(WINDOW, WINDOW + turn)) == set(FINDINGS)


def test_two_runs_in_one_window_take_the_same_share_whatever_order_the_findings_come_in() -> None:
    first = drain.share_of(FINDINGS, cap=CAP, window=WINDOW, name=str)

    assert drain.share_of(FINDINGS[::-1], cap=CAP, window=WINDOW, name=str) == first


def test_a_finding_that_goes_leaves_every_other_one_taken_within_one_more_turn_of_the_set() -> None:
    gone = drain.share_of(FINDINGS, cap=CAP, window=WINDOW, name=str)[0][0]
    left = [name for name in FINDINGS if name != gone]
    turn = math.ceil(len(left) / CAP)

    assert _taken(left, range(WINDOW + 1, WINDOW + 1 + turn)) == set(left)
