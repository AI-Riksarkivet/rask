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

from medallion.api import cascade_lag_cron
from medallion.services.cascade_lag import AbsentEdgeMemo, ConsumedRange, EdgeNotMeasurable, LagTickReport, run_lag_tick


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

    assert report.destination_invisible == [("silver->gold", "advref31")], (
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
    assert report.destination_invisible == []


def test_a_source_that_exists_but_never_published_is_not_reported() -> None:
    """A source table with no ``published`` tag has nothing to fall behind, so its destination's
    absence is expected rather than a loss. Reporting it would fire on every freshly created lane."""
    report = run_lag_tick(
        edges=[("silver->gold", "brand-new")],
        published=lambda edge, project: None,
        consumed=_invisible_destination,
        gauge=_Gauge(),
    )

    assert report.destination_invisible == []
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
    assert report.destination_invisible == [("silver->gold", "advref31")]


def test_the_lag_cron_publishes_the_blind_lanes_it_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """The report carrying the identities is not enough — a series is what an alert can reach.

    The tick publishes the LAG gauge itself and the cron publishes this one, which is the single
    asymmetry between them. An asymmetry nothing pins is how a signal ends up computed, returned, and
    never exported.
    """
    published: list[tuple[str, str]] = []

    def _tick(**_: object) -> LagTickReport:
        return LagTickReport(edges=1, published_points=0, unknown=0, failed=0, destination_invisible=[("silver->gold", "advref31")])

    monkeypatch.setattr(cascade_lag_cron, "run_lag_tick", _tick)
    monkeypatch.setattr(cascade_lag_cron, "record_destination_invisible", published.extend)
    settings = SimpleNamespace(transform_routes={}, lag_projects=[], control_root="", lane_destinations={})

    report = asyncio.run(cascade_lag_cron._on_cron(cast(Any, settings), None))

    assert report.destination_invisible == [("silver->gold", "advref31")]
    assert published == [("silver->gold", "advref31")], "a blind lane the tick found and the cron did not export is a finding nothing can alert on"
