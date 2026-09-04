"""How far behind its source a destination tier has fallen — the cascade's LOSS detector.

`open_cascade_repair.md` C3. C4 alerts on refusals, which are triggers that ARRIVED and were dropped;
this is the other class and the one O2 names. A trigger that never arrived increments no counter, runs
no `_preflight`, writes no log and parks on no DLQ — the only evidence it is missing is that the source
moved and the destination did not.

THE PREDICATE has two halves and both exist only as of `498b5531`: the source's `published` tag version
(the catalog knows it) against the highest source version the destination has consumed (the `lance` run
facet records it). Before that commit the second half had no store at all, which is why this module
could not have been written first.

PURE, over values the caller reads. The two stores are a catalog and a lineage graph, and neither
belongs in a lag calculation — separating them is what makes a first-ever hop, an unpublished source
and a backwards tag drivable in a unit test.

A LAG IS A LEVEL, NOT AN EVENT: true continuously, read by asking. So it is a GAUGE evaluated with a
`for:` clause, never a per-tick counter or log line — row 23 of `open_estate-verification.md` is what
that mistake costs, one gap counted 1210 times and every other service's errors buried.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Protocol

from pydantic import BaseModel


log = logging.getLogger(__name__)


class EdgeLag(BaseModel):
    """One declared edge's distance behind its source.

    ``known=False`` with ``lag=None`` is a first-class answer, not an error: the two stores disagreed or
    one could not be read. It must never collapse to 0, because 0 is what a HEALTHY edge reports and a
    failed read borrowing it turns an outage into a clean bill of health — the same refusal
    `maintenance/services/reconcile.py` makes by keeping unavailable categories out of its counts
    rather than zeroing them.
    """

    edge: str
    project: str
    lag: int | None
    known: bool


def lag_for_edge(*, edge: str, project: str, published: int | None, consumed: int | None) -> EdgeLag:
    """The distance between what the source has published and what the destination has consumed.

    Four shapes, and each is a decision rather than arithmetic:

    * **nothing published** — there is nothing to fall behind, so lag 0 and KNOWN. Reporting it unknown
      would make every fresh estate look broken on its first tick;
    * **published, nothing consumed** — the first-ever hop, and the shape O2 is about. The lag is the
      whole published version: a hop that never happened is exactly what this exists to surface;
    * **consumed ahead of published** — the stores disagree (a tag moved backwards, or a lineage row
      outlived the table it names). UNKNOWN, because a negative lag renders as "very healthy" on every
      dashboard;
    * **a source that could not be read while the destination could** — UNKNOWN for the reason on
      :class:`EdgeLag`.
    """
    if published is None:
        # Nothing published and nothing consumed is an idle, healthy edge. Nothing published while
        # something WAS consumed means the source read failed — the destination cannot have consumed a
        # version that was never published.
        return EdgeLag(edge=edge, project=project, lag=None if consumed is not None else 0, known=consumed is None)
    if consumed is None:
        return EdgeLag(edge=edge, project=project, lag=published, known=True)
    if consumed > published:
        return EdgeLag(edge=edge, project=project, lag=None, known=False)
    return EdgeLag(edge=edge, project=project, lag=published - consumed, known=True)


class LagGauge(Protocol):
    """The one method this module needs of a gauge — injected so the recording rule is drivable.

    A Protocol rather than the OTel instrument type because the rule under test is *when a point is
    published at all*, and asserting that against a real meter would mean standing up a provider and a
    reader to observe an absence.
    """

    def set(self, value: int, attributes: dict[str, str] | None = None) -> None: ...


def record_edge_lag(lag: EdgeLag, *, gauge: LagGauge) -> None:
    """Publish one edge's lag, or publish NOTHING when it is unknown.

    THE SILENCE IS THE POINT. A gauge has no "unknown", so every sentinel becomes a number somebody
    reads: ``-1`` renders as a dip, and ``0`` renders as perfect health — which is exactly what a
    HEALTHY edge reports, so an unreadable catalog would present as a clean bill of health. Publishing
    nothing leaves the series STALE, and staleness is a condition an alert can express while "this
    zero is a lie" is not.

    Zero itself is published, and that is not in tension with the above: a measured 0 means the
    destination is level with its source, which is the normal state and must be visible as such.

    Attributes are BOUNDED by construction — ``edge`` comes from the declared lane set and ``project``
    from the registry. Neither is caller-supplied, which is what keeps this series finite.
    """
    if not lag.known or lag.lag is None:
        return
    gauge.set(lag.lag, {"lance.medallion.edge": lag.edge, "lance.medallion.project": lag.project})


class LagTickReport(BaseModel):
    """What one tick measured. ``edges`` is the denominator every other field is read against.

    ``unknown`` and ``failed`` are separate on purpose: unknown means the stores were read and
    DISAGREED, failed means one could not be read at all. Folding them would hide a systematic outage
    inside what looks like ordinary disagreement.
    """

    edges: int
    published_points: int
    unknown: int
    failed: int


#: Reads the source's published version for one edge. Raising is expected and contained per edge.
VersionReader = Callable[[str, str], int | None]


def run_lag_tick(
    *,
    edges: Sequence[tuple[str, str]],
    published: VersionReader,
    consumed: VersionReader,
    gauge: LagGauge,
) -> LagTickReport:
    """Measure every declared edge, publish what is known, and report what was not.

    READ FAILURES ARE CONTAINED PER EDGE. One unreadable table must not blank the estate: the other
    edges' answers are still true, and abandoning them would turn a single bad table into an
    estate-wide silence that reads exactly like a healthy idle cascade. It is the discipline
    ``maintenance/services/reconcile.py`` already applies — an unavailable category stays OUT of the
    counts while the categories that completed still report.

    THE EVERY-REPLICA ANSWER IS CONVERGENCE. ``bindings.cron`` fires on every replica with no lease, and
    a new cron owes that question an answer rather than an oversight. This tick is READ-ONLY and
    idempotent: two replicas compute the same lag and set the same level, which is precisely what a
    gauge tolerates. So no lock, no ``replicas: 1`` pin and no dedupe key — unlike lineage's reconciler,
    which takes an advisory lock because it WRITES.
    """
    report = LagTickReport(edges=len(edges), published_points=0, unknown=0, failed=0)
    for edge, project in edges:
        try:
            lag = lag_for_edge(edge=edge, project=project, published=published(edge, project), consumed=consumed(edge, project))
        except Exception as exc:  # noqa: BLE001 — one edge's read must never end the tick
            log.warning("cascade_lag_edge_unreadable", extra={"edge": edge, "project": project, "error": str(exc)})
            report.failed += 1
            continue
        if not lag.known:
            report.unknown += 1
            continue
        record_edge_lag(lag, gauge=gauge)
        report.published_points += 1
    log.info(
        "cascade_lag_tick",
        extra={"edges": report.edges, "published": report.published_points, "unknown": report.unknown, "failed": report.failed},
    )
    return report
