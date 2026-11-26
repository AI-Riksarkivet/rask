"""Which findings a capped pass takes in one run, so that none holds a slot run after run.

The tombstone sweep, the drift repair and the tuple rebuild each act on at most a cap of their report's findings. A
finding refused for good (a trash record whose place still holds bytes, a ghost a registry still names, a warehouse
whose record justifies no edge) is in every report, so a fixed order hands it the same slot every run and nothing behind
it is ever tried (measured 2026-11-26 on the sweep: cap 2, two records holding bytes ahead of an empty one, three runs
each refused the same two). The purge and the release avoid it by sorting on the refusal count their trash records carry
(``purge.drain_order``); these three have no such count, since nothing records a refusal against a ghost or a
warehouse, so they take turns instead (:func:`share_of`): the findings stand in an order no run chooses, and each window
of the audit's schedule takes the next ``cap`` of them. No position is stored between runs: the turn is counted from
the clock (:func:`window_of`).
"""

import hashlib
from collections.abc import Callable, Sequence
from datetime import datetime


def window_of(checked_at: str, *, period_seconds: int) -> int:
    """The window of the audit's schedule a report made at ``checked_at`` falls in: the whole periods since the epoch."""
    return int(datetime.fromisoformat(checked_at).timestamp()) // period_seconds


def share_of[Finding](findings: Sequence[Finding], *, cap: int, window: int, name: Callable[[Finding], str]) -> tuple[list[Finding], int]:
    """The findings the runs of ``window`` take, at most ``cap``, and how many they leave for a later one.

    Under the cap every finding is taken, in the order given. Over it, the findings are ordered by a digest of each
    one's ``name``, whatever order they were given in, and the share is the ``cap`` of them from position
    ``window * cap`` on, wrapping at the end. So:

    * a set of ``n`` findings that stays as it is is taken whole in ``ceil(n / cap)`` consecutive windows, whichever
      findings are refused;
    * two runs in one window (a dry run and the armed run after it, the audit and the scan of one hour) take the same
      share of the same findings;
    * a finding that appears or goes shifts the findings behind it in the order by one place, and moves where the
      window's share starts, since the position wraps at the set's size. A finding can then wait out one more turn of
      the whole set, counted from the last window the set changed in; a set that changes every window is promised no
      turn, and none of its findings is handed the same slot run after run either.

    The count of turns holds for a schedule that fires once a window. One that fires every ``k`` windows steps ``k``
    shares at a time, and takes the whole set only where the positions it visits are no more than ``cap`` apart.

    Args:
        findings: What the report found, each a candidate.
        cap: The most one run acts on.
        window: The window of the audit's schedule the report was made in (:func:`window_of`).
        name: What tells one finding from another across runs.
    """
    if len(findings) <= cap:
        return list(findings), 0
    ordered = sorted(findings, key=lambda finding: hashlib.sha256(name(finding).encode()).digest())
    first = (window * cap) % len(ordered)
    return [ordered[(first + turn) % len(ordered)] for turn in range(cap)], len(ordered) - cap
