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
    BLIND_REASONS,
    DESTINATION_INVISIBLE,
    STORES_DISAGREE,
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


def test_both_stores_refusing_is_still_unmeasurable() -> None:
    """The other direction, and the one that keeps this from becoming 252 pages a tick.

    A project that does not run a lane has neither a source nor a destination. Nothing about it is
    wrong, and the tick must stay silent about it.

    This is also the honest limit of the discriminator, recorded rather than glossed: an UNGOVERNED
    source refuses exactly as an absent one does, so a lane that IS running reads as one that is not.
    Measured 2026-09-11: eight cells have an ungoverned source, of which three were dropped and three
    were never written — two are genuinely running and unseen. No reading of the two refusals separates them —
    lineage and the catalog both answer one status for "absent" and "not yours" on purpose — so the
    repair is to govern the table, and the cost of not doing so is that its lane has no series.
    """

    def _invisible_source(edge: str, project: str) -> int | None:
        raise EdgeNotMeasurable(f"{project}-silver$features is not visible to this subject")

    report = run_lag_tick(
        edges=[("silver->gold", "a-tenant-that-does-not-run-this-lane")],
        published=_invisible_source,
        consumed=_invisible_destination,
        gauge=_Gauge(),
    )

    assert report.unmeasurable == 1
    assert report.blind == []


def test_a_source_that_exists_but_never_published_is_not_reported() -> None:
    """A source table with no ``published`` tag has nothing to fall behind, so its destination's
    absence is expected rather than a loss. Reporting it would fire on every freshly created lane."""
    report = run_lag_tick(
        edges=[("silver->gold", "brand-new")],
        published=lambda edge, project: None,
        consumed=_invisible_destination,
        gauge=_Gauge(),
    )

    assert report.blind == []
    assert report.unmeasurable == 1


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


def test_a_disagreement_between_the_two_stores_is_NAMED_not_merely_counted() -> None:
    """The second way a running lane goes unmeasured, and it was counted without an identity.

    Both stores answer and CONTRADICT each other — a frontier ahead of the source's published version,
    which means a tag moved backwards or a lineage run outlived the table it names. `record_edge_lag`
    correctly publishes no point (every sentinel a gauge could carry is also a real lag), so the cell
    has no series, and a count in a log line names no edge. On the live estate 2026-09-11 exactly one
    cell has been in this state on every tick, and nothing in the system can say which.
    """
    report = run_lag_tick(
        edges=[("silver->gold", "acme")],
        published=lambda edge, project: 3,
        consumed=lambda edge, project: [ConsumedRange(from_version=None, to_version=8)],
        gauge=_Gauge(),
    )

    assert report.blind == [BlindEdge(edge="silver->gold", project="acme", reason=STORES_DISAGREE)]
    assert report.published_points == 0, "a contradicted lag must still publish no level — the gauge has no honest value here"


def test_the_two_blind_reasons_are_one_vocabulary_not_two_mechanisms() -> None:
    """Both states mean "this lane is running and I cannot state its lag", and they are reported as one
    list with a CLOSED reason, the way `medallion.stage.refused` already handles its four refusals.

    Two parallel fields would be the shape that caused this in the first place — two sibling readers
    classifying the same refusal differently, so one identical 403 was silent on one side of a file and
    a warning on the other.
    """
    report = run_lag_tick(
        edges=[("silver->gold", "advref31"), ("bronze->silver", "acme")],
        published=lambda edge, project: 3,
        consumed=lambda edge, project: _invisible_destination(edge, project) if project == "advref31" else [ConsumedRange(from_version=None, to_version=8)],
        gauge=_Gauge(),
    )

    assert {(b.reason, b.project) for b in report.blind} == {(DESTINATION_INVISIBLE, "advref31"), (STORES_DISAGREE, "acme")}
    assert {b.reason for b in report.blind} <= BLIND_REASONS, (
        "the reason vocabulary is closed — an unbounded label would publish caller-chosen strings as series"
    )


def test_a_reason_outside_the_vocabulary_cannot_be_constructed() -> None:
    """The bound is a CONTROL, not a comment. `reason` becomes a metric label, and an unbounded label is
    an unbounded series — the estate's standing cardinality rule. A vocabulary that only a docstring
    enforces is one edit away from not being a vocabulary."""
    with pytest.raises(ValidationError):
        BlindEdge(edge="silver->gold", project="acme", reason="something_someone_added_later")


def test_a_cell_that_stops_being_invisible_and_starts_disagreeing_is_not_left_skipped() -> None:
    """The memo asks about ANSWERING, and a disagreement is an answer.

    A cell can move between states — a destination is created, so it stops being invisible and starts
    contradicting its source. If the memo only forgave cells whose lag came out KNOWN, such a cell would
    keep the misses it earned while invisible and stay skipped until the twenty-tick re-probe: silent for
    exactly the reason `blind` exists.
    """
    memo = AbsentEdgeMemo()
    edges = [("silver->gold", "acme")]
    invisible = True

    def _consumed(edge: str, project: str) -> list[ConsumedRange]:
        if invisible:
            raise EdgeNotMeasurable("not visible yet")
        return [ConsumedRange(from_version=None, to_version=9)]

    for _ in range(AbsentEdgeMemo.MISSES_BEFORE_SKIP):
        run_lag_tick(edges=edges, published=lambda edge, project: None, consumed=_consumed, gauge=_Gauge(), memo=memo)
    assert memo.should_skip(("silver->gold", "acme")), "the setup must actually reach the skipping state, or this proves nothing"

    invisible = False
    memo.reset()
    report = run_lag_tick(edges=edges, published=lambda edge, project: 2, consumed=_consumed, gauge=_Gauge(), memo=memo)
    assert report.blind == [BlindEdge(edge="silver->gold", project="acme", reason=STORES_DISAGREE)]

    for _ in range(AbsentEdgeMemo.MISSES_BEFORE_SKIP + 1):
        report = run_lag_tick(edges=edges, published=lambda edge, project: 2, consumed=_consumed, gauge=_Gauge(), memo=memo)
    assert report.skipped == 0, "a cell that answers every tick must never accumulate misses"
    assert report.blind == [BlindEdge(edge="silver->gold", project="acme", reason=STORES_DISAGREE)]
