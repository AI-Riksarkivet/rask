"""How far behind its source a destination tier has fallen — the cascade's LOSS detector.

docs/DECISIONS.md "Cascade repair" (C3). C4 alerts on refusals, which are triggers that ARRIVED and were dropped;
this is the other class and the one O2 names. A trigger that never arrived increments no counter, runs
no `_preflight`, writes no log and parks on no DLQ — the only evidence it is missing is that the source
moved and the destination did not.

THE PREDICATE has two halves and both exist only as of `498b5531`: the source's `published` tag version
(the catalog knows it) against the highest CONTIGUOUS source version the destination has consumed (the
`lance` run facet records each run's range). Before that commit the second half had no store at all,
which is why this module could not have been written first.

CONTIGUITY IS WHAT MAKES THIS A LOSS DETECTOR. A consumer resolves its delta as
`_row_created_at_version > from AND <= to`, and `from` is the source's PREVIOUS PUBLISHED version
(`publication.py`) rather than the previous CONSUMED one — so the rows a lost trigger skipped fall
outside every later hop's filter and are never read again. Measured against the highest `to_version`,
such an edge reads level the moment any later hop succeeds, while the skipped rows are gone; measured
against the frontier, the gap stays visible until the missed range is genuinely re-driven, and then
clears on the next tick.

AND THE FRONTIER IS ANCHORED AT THE OBSERVED WINDOW, not at version 0 — see `consumed_frontier`.
Lineage prunes runs, so a detector that demanded coverage from the beginning of time would report a
large permanent lag on every healthy edge as soon as retention passed. What it claims is bounded by
what the graph still holds.

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

from pydantic import BaseModel, ConfigDict


log = logging.getLogger(__name__)


class EdgeNotMeasurable(Exception):
    """This subject cannot SEE the edge's source table — and asking again will not change that.

    Distinct from a failure on purpose. The catalog answers one status for "no such table" and "not
    yours" (`rask-lance-catalog`, "NO EXISTENCE ORACLE": distinguishing them would let a door enumerate
    ids), and its authz gate runs before existence resolution, so an absent table answers 403. A
    detector reading that cannot honestly claim either "idle" or "broken".

    Neither of the two existing outcomes fits. Counted FAILED, an estate holding abandoned projects
    reports hundreds of permanent failures per tick and buries a real outage in them — the repeating-
    condition noise row 23 of `open_estate-verification.md` cost. Read as "never published" it becomes
    lag 0, a fabricated healthy series for a cascade that does not exist. So it is its own count, and
    publishes nothing.
    """


class ConsumedRange(BaseModel):
    """One run's delta boundary — the source versions it actually read.

    ``from_version`` is EXCLUSIVE and matches the filter the stage applies; ``None`` means a first
    publication, i.e. everything up to ``to_version``. Carried as ``None`` rather than 0 for the reason
    `build_stage_trigger` states on the wire: "no prior publication" and "published from version 0" are
    different claims, and only the first covers the start of the dataset.
    """

    model_config = ConfigDict(frozen=True)

    from_version: int | None
    to_version: int


def consumed_frontier(ranges: Sequence[ConsumedRange]) -> int:
    """The highest source version reached by an unbroken chain of consumed ranges, from the EARLIEST
    range observed rather than from version 0.

    Ranges are half-open ``(from, to]`` and may overlap — a re-run re-consumes a range an earlier run
    already covered, so overlap is the normal shape after any repair and reads as coverage. A range
    whose lower bound sits ABOVE the frontier is a gap: the versions between were published and never
    read, and no later run will read them.

    ANCHORED AT THE EARLIEST OBSERVED LOWER BOUND, which is what makes this survivable in production.
    Lineage prunes run nodes (`LINEAGE_RUN_RETENTION_DAYS` → `PRUNE_OLD_RUNS_TEMPLATE`, a
    `DETACH DELETE` under the reconcile lock), so anchoring at 0 would turn every healthy edge into a
    permanent large lag once retention passes — measured on this estate at 86 and 115 versions for
    edges that had lost nothing. The detector therefore claims only what the retained evidence
    supports: a gap BETWEEN observed runs is a loss, while the window before the earliest observed run
    is unobservable, because "never consumed" and "the run that consumed it was pruned" are the same
    picture. A loss older than retention is consequently invisible here — and also unrepairable, since
    the re-run verb needs those versions to re-drive.

    Returns 0 for no ranges, which the caller turns into the full-distance first-hop lag.
    """
    if not ranges:
        return 0
    lowers = [0 if span.from_version is None else span.from_version for span in ranges]
    frontier = min(lowers)
    # `None` sorts first as -1: it covers from the start of the dataset, so it can only extend.
    for span in sorted(ranges, key=lambda r: (-1 if r.from_version is None else r.from_version, r.to_version)):
        lower = 0 if span.from_version is None else span.from_version
        if lower > frontier:
            break  # (frontier, lower] was published and never consumed — every later range is behind it
        frontier = max(frontier, span.to_version)
    return frontier


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


def lag_for_edge(*, edge: str, project: str, published: int | None, consumed: Sequence[ConsumedRange]) -> EdgeLag:
    """The distance between what the source has published and what the destination has contiguously read.

    Four shapes, and each is a decision rather than arithmetic:

    * **nothing published** — there is nothing to fall behind, so lag 0 and KNOWN. Reporting it unknown
      would make every fresh estate look broken on its first tick;
    * **published, nothing consumed** — the first-ever hop, and the shape O2 is about. The lag is the
      whole published version: a hop that never happened is exactly what this exists to surface;
    * **the frontier ahead of published** — the stores disagree (a tag moved backwards, or a lineage row
      outlived the table it names). UNKNOWN, because a negative lag renders as "very healthy" on every
      dashboard;
    * **a source that could not be read while the destination could** — UNKNOWN for the reason on
      :class:`EdgeLag`.

    A GAP IS NOT ITS OWN OUTCOME, deliberately: it reports as the lag it causes. The alternative — a
    second signal for "lossy" beside the level — would need its own gauge, its own alert and its own
    `for:` clause to say something the level already says, and would let an operator silence the lag
    while the loss stood. One number, and it stays non-zero until the missed range is re-driven.
    """
    frontier = consumed_frontier(consumed)
    if published is None:
        # Nothing published and nothing consumed is an idle, healthy edge. Nothing published while
        # something WAS consumed means the source read failed — the destination cannot have consumed a
        # version that was never published.
        return EdgeLag(edge=edge, project=project, lag=None if consumed else 0, known=not consumed)
    if not consumed:
        return EdgeLag(edge=edge, project=project, lag=published, known=True)
    if frontier > published:
        return EdgeLag(edge=edge, project=project, lag=None, known=False)
    return EdgeLag(edge=edge, project=project, lag=published - frontier, known=True)


class LagGauge(Protocol):
    """The one method this module needs of a gauge — injected so the recording rule is drivable.

    A Protocol rather than the OTel instrument type because the rule under test is *when a point is
    published at all*, and asserting that against a real meter would mean standing up a provider and a
    reader to observe an absence.
    """

    #: POSITIONAL-ONLY, and that is not cosmetic: OTel's own `Gauge.set` names its first parameter
    #: `amount`, so a by-name protocol would be satisfied by this module's test double and by NOTHING
    #: in production — the shape where the suite is green and the deployed call raises. `ty` reports it
    #: at the handing-out site; the marker is what makes the real instrument conform.
    def set(self, value: int, /, attributes: dict[str, str] | None = None) -> None: ...


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

    ``unknown``, ``failed`` and ``unmeasurable`` are three separate counts and each hides a different
    thing when folded. UNKNOWN means both stores answered and DISAGREED; FAILED means one could not be
    read at all, and a rising count is an outage; UNMEASURABLE means this subject cannot see the
    source table, which is a steady state rather than an event — an estate holding abandoned projects
    reports hundreds every tick, and letting those land in ``failed`` buries a real outage in them.
    """

    edges: int
    published_points: int
    unknown: int
    failed: int
    unmeasurable: int = 0


#: Reads the source's published version for one edge. Raising is expected and contained per edge.
PublishedReader = Callable[[str, str], int | None]

#: Reads every delta range the DESTINATION consumed for one edge — not a single ceiling, because the
#: gap between two ranges is the loss this detector exists to find. An empty sequence means the
#: destination has never run; a read that cannot answer raises, and `run_lag_tick` contains it.
ConsumedReader = Callable[[str, str], Sequence[ConsumedRange]]


def run_lag_tick(
    *,
    edges: Sequence[tuple[str, str]],
    published: PublishedReader,
    consumed: ConsumedReader,
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
        except EdgeNotMeasurable:
            # No log line: this is a steady state, not an event, and one line per invisible edge per
            # tick is the shape that buried every other service's errors once already.
            report.unmeasurable += 1
            continue
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
