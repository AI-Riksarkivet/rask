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

from typing import Protocol

from pydantic import BaseModel


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
