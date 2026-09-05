"""Per-edge cascade lag: how far behind its source a destination tier has fallen.

docs/DECISIONS.md "Cascade repair" (C3), and the ONLY thing that can see the LOSS class. C4 alerts on refusals —
triggers that ARRIVED and were dropped — but a trigger that never arrived increments nothing, runs no
`_preflight`, writes no log and parks on no DLQ. The only evidence such a hop is missing is that the
source moved and the destination did not.

THE PREDICATE, and both halves exist only as of C3a: the source's `published` tag version (the catalog
knows it) against the highest CONTIGUOUS source version the destination has consumed (the `lance` run
facet records each run's range, since `498b5531`). Before C3a the second half had no store at all.

CONTIGUOUS, not the high-water mark, and that is the whole of what this detector can see. A consumer
resolves its delta as `_row_created_at_version > from AND <= to`, where `from` is the source's PREVIOUS
PUBLISHED version — so a lost trigger's rows fall outside every later hop's filter and are never read
again. Measure against `max(to_version)` and the edge reads level the moment any later hop succeeds,
while the skipped rows are gone for good; measure against the frontier and the gap stays visible as a
lag that no later success can clear, until the missed range is actually re-driven.

PURE over injected readers, because the two stores are a catalog and a lineage graph and neither
belongs in a lag calculation. It is also what lets the interesting cases be driven at all — a first-ever
hop, a source that has never published, a destination that has consumed a version the source has since
passed.

NEVER A COUNTER, NEVER A LOG LINE. A lag is a level, not an event: it is true continuously and is read
by asking, so it is a GAUGE evaluated with `for:`. docs/DECISIONS.md "A repeating condition is a LEVEL, not an event" is the
lesson — a repeating condition emitted per tick counted one gap 1210 times and buried every other
service's errors.
"""

from __future__ import annotations

from medallion.services.cascade_lag import ConsumedRange, EdgeLag, lag_for_edge


def _consumed(*ranges: tuple[int | None, int]) -> list[ConsumedRange]:
    """The runs a destination recorded, as the graph returns them: `(from_version, to_version)` pairs,
    `None` meaning a first publication that consumed everything up to `to`."""
    return [ConsumedRange(from_version=lo, to_version=hi) for lo, hi in ranges]


def test_a_destination_level_with_its_source_has_no_lag() -> None:
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=7, consumed=_consumed((None, 3), (3, 7)))
    assert lag == EdgeLag(edge="bronze->silver", project="acme", lag=0, known=True)


def test_a_destination_behind_its_source_reports_the_distance() -> None:
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=9, consumed=_consumed((None, 7)))
    assert lag.lag == 2
    assert lag.known is True


def test_a_source_that_never_published_is_not_a_lag() -> None:
    """Nothing to fall behind. Zero, not unknown: the edge is healthy and idle, and reporting it
    unknown would make every fresh estate look broken."""
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=None, consumed=[])
    assert lag == EdgeLag(edge="bronze->silver", project="acme", lag=0, known=True)


def test_a_source_published_with_nothing_consumed_is_the_FULL_distance() -> None:
    """The first-ever hop, and the shape O2 is about: the source published and the destination has
    never run. The lag is the whole published version, not unknown — a hop that never happened is
    exactly what this must surface."""
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=5, consumed=[])
    assert lag.lag == 5
    assert lag.known is True


def test_a_destination_AHEAD_of_its_source_is_reported_UNKNOWN_not_negative() -> None:
    """Consumed > published means the two stores disagree — a re-published tag moved backwards, or a
    lineage row outlived the table it names. A negative lag would render as "very healthy" on every
    dashboard; unknown is the honest answer and is what an operator must be shown."""
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=3, consumed=_consumed((None, 8)))
    assert lag.known is False
    assert lag.lag is None


def test_an_unreadable_source_is_UNKNOWN_rather_than_zero() -> None:
    """A catalog that cannot be read must never render as "no lag". Zero is the value a healthy edge
    reports, so a failed read borrowing it turns an outage into a clean bill of health — the precise
    failure `maintenance/services/reconcile.py` refuses by keeping unavailable categories OUT of its
    counts rather than zeroing them."""
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=None, consumed=_consumed((None, 4)))
    assert lag.known is False
    assert lag.lag is None


def test_A_LOST_HOP_STAYS_VISIBLE_AFTER_A_LATER_HOP_SUCCEEDS() -> None:
    """THE DEFECT THIS DETECTOR EXISTS FOR, and the one the high-water mark could not see.

    bronze publishes v5 (from=3) and the trigger is lost. bronze publishes v8 (from=5); silver's job
    reads `_row_created_at_version > 5`, so the rows created in (3,5] are excluded from that filter and
    from every filter after it. Against `max(to_version)` the edge reports published=8, consumed=8,
    lag 0 — level, healthy, and permanently wrong about 5 versions' worth of rows that no later hop
    will ever read. Against the frontier the coverage stops at 3 and the lag is 5.
    """
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=8, consumed=_consumed((None, 3), (5, 8)))
    assert lag.known is True
    assert lag.lag == 5, "a gap in the consumed ranges read as level — the loss detector cannot see a loss"


def test_the_lag_CLEARS_when_the_missed_range_is_actually_re_driven() -> None:
    """The other half, and what makes the number actionable rather than a permanent red mark: the
    re-run verb re-drives (3,5], the coverage becomes contiguous, and the edge goes level on the next
    tick. A detector that could not clear would be a detector nobody keeps."""
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=8, consumed=_consumed((None, 3), (5, 8), (3, 5)))
    assert lag == EdgeLag(edge="bronze->silver", project="acme", lag=0, known=True)


def test_overlapping_ranges_are_coverage_not_a_gap() -> None:
    """A re-run re-consumes a range an earlier run already covered, so overlap is the NORMAL shape
    after any repair. Treating it as a gap would make every repaired edge look broken."""
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=9, consumed=_consumed((None, 5), (3, 9)))
    assert lag == EdgeLag(edge="bronze->silver", project="acme", lag=0, known=True)


def test_the_WINDOW_BEFORE_the_earliest_observed_run_is_not_claimed_as_a_gap() -> None:
    """The detector speaks only to what the graph still holds, and this is the line that keeps it usable.

    Lineage PRUNES run nodes — `LINEAGE_RUN_RETENTION_DAYS` drives `PRUNE_OLD_RUNS_TEMPLATE`, a
    `DETACH DELETE` under the reconcile cron's lock. Anchoring coverage at version 0 therefore turns
    every healthy edge into a permanent large lag the moment retention passes, because the runs that
    covered the early versions are simply gone. Measured on this estate before the fix: `silver->gold`
    for `acme` reported 86 and for `lakehouse` 115, from edges that had lost nothing.

    A detector that cries loss on every edge forever is one nobody reads, so the claim is narrowed to
    what the evidence supports: a gap BETWEEN two observed runs is a real loss; the window before the
    earliest observed run is UNOBSERVABLE, and "never consumed" cannot be told apart from "the run
    that consumed it was pruned". The cost is stated rather than hidden — a loss older than retention
    is invisible here, and it is also unrepairable by the re-run verb, which needs those versions.
    """
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=9, consumed=_consumed((4, 9)))
    assert lag == EdgeLag(edge="bronze->silver", project="acme", lag=0, known=True)


def test_a_gap_between_observed_runs_survives_a_pruned_PREFIX() -> None:
    """The property that must hold once both rules are in play: retention removes the early runs, and a
    genuine hole between the runs that REMAIN is still reported."""
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=20, consumed=_consumed((10, 14), (16, 20)))
    assert lag.lag == 6, "a gap between two surviving runs was hidden by the pruned-prefix allowance"
