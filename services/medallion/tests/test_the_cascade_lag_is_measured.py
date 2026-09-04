"""Per-edge cascade lag: how far behind its source a destination tier has fallen.

`open_cascade_repair.md` C3, and the ONLY thing that can see the LOSS class. C4 alerts on refusals —
triggers that ARRIVED and were dropped — but a trigger that never arrived increments nothing, runs no
`_preflight`, writes no log and parks on no DLQ. The only evidence such a hop is missing is that the
source moved and the destination did not.

THE PREDICATE, and both halves exist only as of C3a: the source's `published` tag version (the catalog
knows it) against the highest source version the destination has actually consumed (the `lance` run
facet records it, since `498b5531`). Before C3a the second half had no store at all.

PURE over injected readers, because the two stores are a catalog and a lineage graph and neither
belongs in a lag calculation. It is also what lets the interesting cases be driven at all — a first-ever
hop, a source that has never published, a destination that has consumed a version the source has since
passed.

NEVER A COUNTER, NEVER A LOG LINE. A lag is a level, not an event: it is true continuously and is read
by asking, so it is a GAUGE evaluated with `for:`. Row 23 of `open_estate-verification.md` is the
lesson — a repeating condition emitted per tick counted one gap 1210 times and buried every other
service's errors.
"""

from __future__ import annotations

from medallion.services.cascade_lag import EdgeLag, lag_for_edge


def test_a_destination_level_with_its_source_has_no_lag() -> None:
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=7, consumed=7)
    assert lag == EdgeLag(edge="bronze->silver", project="acme", lag=0, known=True)


def test_a_destination_behind_its_source_reports_the_distance() -> None:
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=9, consumed=7)
    assert lag.lag == 2
    assert lag.known is True


def test_a_source_that_never_published_is_not_a_lag() -> None:
    """Nothing to fall behind. Zero, not unknown: the edge is healthy and idle, and reporting it
    unknown would make every fresh estate look broken."""
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=None, consumed=None)
    assert lag == EdgeLag(edge="bronze->silver", project="acme", lag=0, known=True)


def test_a_source_published_with_nothing_consumed_is_the_FULL_distance() -> None:
    """The first-ever hop, and the shape O2 is about: the source published and the destination has
    never run. The lag is the whole published version, not unknown — a hop that never happened is
    exactly what this must surface."""
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=5, consumed=None)
    assert lag.lag == 5
    assert lag.known is True


def test_a_destination_AHEAD_of_its_source_is_reported_UNKNOWN_not_negative() -> None:
    """Consumed > published means the two stores disagree — a re-published tag moved backwards, or a
    lineage row outlived the table it names. A negative lag would render as "very healthy" on every
    dashboard; unknown is the honest answer and is what an operator must be shown."""
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=3, consumed=8)
    assert lag.known is False
    assert lag.lag is None


def test_an_unreadable_source_is_UNKNOWN_rather_than_zero() -> None:
    """A catalog that cannot be read must never render as "no lag". Zero is the value a healthy edge
    reports, so a failed read borrowing it turns an outage into a clean bill of health — the precise
    failure `maintenance/services/reconcile.py` refuses by keeping unavailable categories OUT of its
    counts rather than zeroing them."""
    lag = lag_for_edge(edge="bronze->silver", project="acme", published=None, consumed=4)
    assert lag.known is False
    assert lag.lag is None
