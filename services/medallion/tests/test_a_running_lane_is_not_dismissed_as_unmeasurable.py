"""A lane whose SOURCE is publishing is running; an unreadable destination there is a finding, not a shrug.

MEASURED ON THE LIVE ESTATE 2026-09-11, and the shape is why this file exists. The cascade-lag tick
declares a cartesian product — every declared lane x every project in the warehouse registry, 3 x 89 =
267 cells — and 252 of them answer "not visible to this subject". That is correct and expected for the
overwhelming majority: the project does not run that lane, so neither its source nor its destination
exists, and there is nothing to measure. `EdgeNotMeasurable` is the honest answer.

BUT ONE CELL IS NOT LIKE THE OTHERS, and folding it in with the 251 is what made a real loss invisible.
`advref31 silver->gold` has a published source (`advref31-silver$features`) and a destination
(`advref31-gold$catalog`) with NO Dataset node in lineage at all — asked of AGE directly, that tenant
has bronze and silver and no gold. The silver->gold hop has never run. That is exactly the loss
`cascade_lag`'s module docstring says it exists to detect, and the detector reported it as nothing.

WHY THE OBVIOUS FIX IS WRONG. Reading the destination's 403 as "absent" and publishing the first-hop
lag would be a fabrication: lineage's `/datasets/{name}/producers` is gated router-level by
`require_metadata_access`, which runs BEFORE existence resolution, so absent and forbidden answer
alike — and this estate holds gold tables that exist with zero authorization tuples, which 403 the
same way. A detector that guessed would publish a confident lag for a hop that had in fact run.

SO THE DISCRIMINATOR IS THE SOURCE SIDE, which is read from a different store and answers for itself.
Both stores refusing means the project does not run the lane. A source that HAS published into a
destination this subject cannot read is a lane that is running and unmeasured — one cell of 267 on
this estate, which is a signal rather than noise. It is reported as its own state, keeps being asked,
and publishes no fabricated lag.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from medallion.api import cascade_lag_cron
from medallion.services.cascade_lag import (
    DESTINATION_INVISIBLE,
    AbsentEdgeMemo,
    BlindEdge,
    ConsumedRange,
    EdgeNotMeasurable,
    LagTickReport,
    run_lag_tick,
)


class _Gauge:
    def __init__(self) -> None:
        self.points: list[tuple[int, dict[str, str]]] = []

    def set(self, amount: int, /, attributes: dict[str, str] | None = None) -> None:
        self.points.append((amount, attributes or {}))


def _invisible_destination(edge: str, project: str) -> list[ConsumedRange]:
    raise EdgeNotMeasurable(f"{project}-gold$catalog is not visible to this subject")


def test_a_published_source_with_an_unreadable_destination_is_reported_not_dismissed() -> None:
    """THE GATE. Before this, the one live cell that names a real loss was counted with the 251 that name nothing."""
    gauge = _Gauge()
    report = run_lag_tick(
        edges=[("silver->gold", "advref31")],
        published=lambda edge, project: 12,
        consumed=_invisible_destination,
        gauge=gauge,
    )

    assert report.blind == [BlindEdge(edge="silver->gold", project="advref31", reason=DESTINATION_INVISIBLE)], (
        "a lane whose source has published is running; its unreadable destination is the loss this detector exists for"
    )
    assert report.unmeasurable == 0, "folding it in with the cells that name nothing is what made it invisible"
    assert gauge.points == [], (
        "and it still publishes NO lag: an absent destination and a forbidden one answer alike, so a "
        "first-hop lag here would be a fabrication for a hop that may have run"
    )


def test_a_source_that_exists_but_never_published_is_not_reported() -> None:
    """A source table with no ``published`` tag has nothing to fall behind, so its destination's
    absence is expected rather than a loss. Reporting it would fire on every freshly created lane, and
    that objection still stands — which is why this cell publishes no series and raises no blind entry.

    WHAT CHANGED IS THE BUCKET, NOT THE SILENCE ([[LH-167]]). The cell used to be counted
    `unmeasurable`, whose own definition is "the SOURCE is not visible" — and this source ANSWERED. So
    a tier written eight times and then stopped was indistinguishable from a lane nobody runs. It now
    lands in `unpublished_source`, carrying its identity, where somebody asking the report gets an
    answer; nothing is pushed, so no freshly created lane pages anyone.
    """
    report = run_lag_tick(
        edges=[("silver->gold", "brand-new")],
        published=lambda edge, project: None,
        consumed=_invisible_destination,
        gauge=_Gauge(),
    )

    assert report.blind == []
    assert report.unmeasurable == 0, "the source answered, so it is not the 'source not visible' state"
    assert [(c.edge, c.project) for c in report.unpublished_source] == [("silver->gold", "brand-new")]


def test_the_reported_cell_keeps_being_asked() -> None:
    """A finding that memoizes itself into silence is the defect this fix replaces, not a fix.

    `AbsentEdgeMemo` exists to stop probing cells that name nothing — it skips after three consecutive
    refusals, and each probe writes an `access_denied` audit record. A cell whose source is publishing
    is not one of those: it is the estate's only evidence of that lost hop, so it pays one audit record
    a tick and stays in the population.
    """
    memo = AbsentEdgeMemo()
    edges = [("silver->gold", "advref31")]
    for _ in range(AbsentEdgeMemo.MISSES_BEFORE_SKIP + 2):
        report = run_lag_tick(
            edges=edges,
            published=lambda edge, project: 12,
            consumed=_invisible_destination,
            gauge=_Gauge(),
            memo=memo,
        )

    assert report.skipped == 0, "the memo must never silence a lane that is publishing into a destination it cannot read"
    assert report.blind == [BlindEdge(edge="silver->gold", project="advref31", reason=DESTINATION_INVISIBLE)]


def test_the_lag_cron_publishes_the_blind_lanes_it_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """The report carrying the identities is not enough — a series is what an alert can reach.

    The tick publishes the LAG gauge itself and the cron publishes this one, which is the single
    asymmetry between them. An asymmetry nothing pins is how a signal ends up computed, returned, and
    never exported.
    """
    published: list[BlindEdge] = []
    found = BlindEdge(edge="silver->gold", project="advref31", reason=DESTINATION_INVISIBLE)

    def _tick(**_: object) -> LagTickReport:
        return LagTickReport(edges=1, published_points=0, failed=0, blind=[found])

    monkeypatch.setattr(cascade_lag_cron, "run_lag_tick", _tick)
    monkeypatch.setattr(cascade_lag_cron, "record_blind_edges", published.extend)
    settings = SimpleNamespace(transform_routes={}, lag_projects=[], control_root="", lane_destinations={})

    report = asyncio.run(cascade_lag_cron._on_cron(cast(Any, settings), None))

    assert report.blind == [found]
    assert published == [found], "a blind lane the tick found and the cron did not export is a finding nothing can alert on"


def test_a_reason_outside_the_vocabulary_cannot_be_constructed() -> None:
    """The bound is a CONTROL, not a comment. `reason` becomes a metric label, and an unbounded label is
    an unbounded series — the estate's standing cardinality rule. A vocabulary that only a docstring
    enforces is one edit away from not being a vocabulary."""
    with pytest.raises(ValidationError):
        BlindEdge(edge="silver->gold", project="acme", reason="something_someone_added_later")


def test_a_cell_that_answers_between_misses_does_not_accumulate_toward_the_skip() -> None:
    """`record_present` is what makes the misses CONSECUTIVE rather than cumulative.

    The memo skips a cell after ``MISSES_BEFORE_SKIP`` refusals IN A ROW. An answer in between has to
    clear the count, or a cell that merely flickers — two misses, an answer, two misses — reaches the
    threshold by addition and goes silent, which is the opposite of what the threshold is for: a tenant
    mid-deploy answers intermittently, and that is exactly the cell the operator still needs measured.

    DRIVEN THROUGH THE TICK, not by calling the memo, because the question is whether the tick tells the
    memo about the answer at all. An earlier version of this test reset the memo mid-scenario and then
    asserted `skipped == 0`; that could not fail, since a reset memo has nothing left to skip. Mutating
    `record_present` to a no-op is the check that matters, and this fails under it.
    """
    memo = AbsentEdgeMemo()
    edges = [("silver->gold", "acme")]
    cell = ("silver->gold", "acme")
    visible = False

    def _consumed(edge: str, project: str) -> list[ConsumedRange]:
        if not visible:
            raise EdgeNotMeasurable("not visible yet")
        return [ConsumedRange(from_version=None, to_version=9)]

    # Two misses: one short of the threshold.
    for _ in range(AbsentEdgeMemo.MISSES_BEFORE_SKIP - 1):
        run_lag_tick(edges=edges, published=lambda edge, project: None, consumed=_consumed, gauge=_Gauge(), memo=memo)
    assert not memo.should_skip(cell), "the setup must stop one short, or the answer below proves nothing"

    # One tick where BOTH stores answer. A disagreement is still an answer.
    visible = True
    run_lag_tick(edges=edges, published=lambda edge, project: 2, consumed=_consumed, gauge=_Gauge(), memo=memo)

    # Two more misses. Consecutively that is two, not four — so the cell must still be measured.
    visible = False
    for _ in range(AbsentEdgeMemo.MISSES_BEFORE_SKIP - 1):
        report = run_lag_tick(edges=edges, published=lambda edge, project: None, consumed=_consumed, gauge=_Gauge(), memo=memo)

    assert not memo.should_skip(cell), "an answering tick must clear the miss count — otherwise a flickering cell is skipped by addition"
    assert report.skipped == 0
