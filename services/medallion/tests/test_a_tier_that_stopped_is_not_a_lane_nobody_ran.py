"""A source that EXISTS and never published is a different answer from a source nobody can see.

[[LH-167]]. A tenant's silver tier was written eight times and published never, and no surface on the
estate says so: `advref31-silver$features` answers `tags/list` **200 `{"tags": {}}`** — the table exists
and is readable — while `advref31-gold$catalog` 404s. Five other tenants in the same graph DO have gold,
so this is one lane that stopped beside five that did not, and the row's Closes-when asks for exactly
one thing: *"a surface names a written-but-unpublished tier."*

THE DETECTOR ALREADY KNOWS, AND SPENDS IT. `run_lag_tick` reaches `unmeasurable` down two different
paths — the source reader RAISING (`EdgeNotMeasurable`: 403/404, the source is not visible) and the
source answering with NO published version (a 200 with an empty tag map). Those are opposite findings:
the first is a lane this project does not run, the second is a lane that ran far enough to create and
write a table and then stopped before publishing. They increment one counter.

**AND `unmeasurable`'S OWN DOCSTRING EXCLUDES THE SECOND**, which is what makes this a defect in the
module's terms rather than a preference: *"UNMEASURABLE means the SOURCE is not visible — usually a
project that does not run this lane."* A visible source is not that. The same docstring already argues
the general case — *"Folded into `unmeasurable` those two are one part in 252 and say nothing, which is
indistinguishable from a healthy lane"* — for `blind`; this is the same argument one field over.

WHY A COUNTER AND NOT A LOG LINE: the tick is a steady state read every cron fire, and this module
records what one-line-per-cell-per-tick did to every other service's errors. A count moves, and a count
that moves from 0 is the signal — a tenant whose tier stopped this week appears, where 252 silent
unmeasurables never would.
"""

from __future__ import annotations

from medallion.services.cascade_lag import ConsumedRange, EdgeNotMeasurable, run_lag_tick


class _Gauge:
    def __init__(self) -> None:
        self.points: list[tuple[int, dict[str, str]]] = []

    def set(self, amount: int, /, attributes: dict[str, str] | None = None) -> None:
        self.points.append((amount, attributes or {}))


EDGE = [("silver->gold", "advref31")]


def _no_destination(edge: str, project: str) -> list[ConsumedRange]:
    """The destination cannot be measured — `advref31-gold$catalog` has no Dataset node at all."""
    raise EdgeNotMeasurable(f"{edge} destination is not visible")


def test_a_source_that_EXISTS_but_never_published_is_counted_apart() -> None:
    """THE DEFECT. Written-but-unpublished must not land in the bucket meaning "no visible source".

    Driven through the reader seam exactly as the tick calls it: `published` RETURNS None (the live
    shape — a 200 with an empty tag map) rather than raising, which is the whole discriminator.
    """
    report = run_lag_tick(edges=EDGE, published=lambda edge, project: None, consumed=_no_destination, gauge=_Gauge())

    assert len(report.unpublished_source) == 1, (
        "a source that exists and has never published was not counted apart — it is indistinguishable from a lane nobody runs, which is the whole of LH-167"
    )
    assert report.unmeasurable == 0, "`unmeasurable` means the SOURCE IS NOT VISIBLE, and this source answered"


def test_a_source_that_CANNOT_BE_SEEN_still_counts_as_unmeasurable() -> None:
    """The other side, pinned so the fix cannot be "rename the counter".

    A raising source reader is a project that does not run this lane — 252 of 267 declared cells on the
    live estate — and it must keep landing in `unmeasurable`, or an estate of abandoned projects starts
    reporting hundreds of stalled tiers a tick.
    """

    def _invisible(edge: str, project: str) -> int | None:
        raise EdgeNotMeasurable(f"{edge} source is not visible")

    report = run_lag_tick(edges=EDGE, published=_invisible, consumed=_no_destination, gauge=_Gauge())

    assert report.unmeasurable == 1
    assert report.unpublished_source == [], "an invisible source is not a stalled tier — nothing says it was ever written"


def test_a_healthy_lane_reports_neither() -> None:
    """Without this the two counters could both be satisfied by a detector that counts every cell."""
    report = run_lag_tick(
        edges=EDGE,
        published=lambda edge, project: 3,
        consumed=lambda edge, project: [ConsumedRange(from_version=None, to_version=3)],
        gauge=_Gauge(),
    )

    assert (report.unpublished_source, report.unmeasurable, report.published_points) == ([], 0, 1)
