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
`for:` clause, never a per-tick counter or log line — docs/DECISIONS.md "A repeating condition is a LEVEL, not an event" is what
that mistake costs, one gap counted 1210 times and every other service's errors buried.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


log = logging.getLogger(__name__)


class EdgeNotMeasurable(Exception):
    """This subject cannot SEE the edge's source table — and asking again will not change that.

    Distinct from a failure on purpose. The catalog answers one status for "no such table" and "not
    yours" (`rask-lance-catalog`, "NO EXISTENCE ORACLE": distinguishing them would let a door enumerate
    ids), and its authz gate runs before existence resolution, so an absent table answers 403. A
    detector reading that cannot honestly claim either "idle" or "broken".

    Neither of the two existing outcomes fits. Counted FAILED, an estate holding abandoned projects
    reports hundreds of permanent failures per tick and buries a real outage in them — the repeating-
    condition noise docs/DECISIONS.md "A repeating condition is a LEVEL, not an event" cost. Read as "never published" it becomes
    lag 0, a fabricated healthy series for a cascade that does not exist. So it is its own count, and
    publishes nothing.
    """


#: A lane whose SOURCE has published into a destination this subject cannot read. Absence and
#: forbiddance answer alike at lineage's door — its metadata gate runs before existence resolution —
#: so the hop may never have run or may have run into a table carrying no tuples.
DESTINATION_INVISIBLE: Final = "destination_invisible"

#: Both stores answered and CONTRADICTED each other: a consumed frontier ahead of the source's
#: published version, which means a tag moved backwards or a lineage run outlived the table it names.
STORES_DISAGREE: Final = "stores_disagree"

#: CLOSED, and closed for the reason `metrics.record_refused` states: a reason label taken from a
#: caller would be an unbounded series, and here the values are decided by this module alone.
BLIND_REASONS: Final = frozenset({DESTINATION_INVISIBLE, STORES_DISAGREE})


class BlindEdge(BaseModel):
    """One declared cell the detector could not state a lag for, and WHY.

    Blind is not the same as unmeasurable, and the near-miss in the words is worth the care: an
    UNMEASURABLE cell has no source, so the project does not run that lane and there is nothing to
    measure — the overwhelming majority, and correctly silent. A BLIND cell has a published source, so
    the lane is running and the detector owes an answer it cannot give.

    Carried as identities rather than counts because the operator's first question is WHICH lane, and
    both fields are bounded by construction — ``edge`` comes from the declared lane map and ``project``
    from the warehouse registry, the same pair `record_edge_lag` labels with.
    """

    model_config = ConfigDict(frozen=True)

    edge: str
    project: str
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_is_in_the_closed_vocabulary(cls, value: str) -> str:
        """ENFORCED, not merely documented. `reason` becomes a metric label, and the estate's rule is
        that a label's values are bounded by construction — prose saying so is the shape that lets a
        later edit publish an unbounded series without anything objecting."""
        if value not in BLIND_REASONS:
            raise ValueError(f"blind reason {value!r} is not one of {sorted(BLIND_REASONS)}")
        return value


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

    ``failed``, ``unmeasurable`` and ``blind`` are three separate states and
    each hides a different thing when folded. FAILED means a store could not be read at all, and a rising
    count is an outage; UNMEASURABLE means the SOURCE is not visible — usually a project that does not
    run this lane, a steady state rather than an event, which an estate holding abandoned projects
    reports hundreds of every tick, and letting those land in ``failed`` buries a real outage in them.
    It also silently covers an UNGOVERNED source, which refuses identically while its lane really is
    running — two such cells, measured 2026-09-11. That one is repaired by governing the table, not by
    anything this module can do.

    ``blind`` IS THE ONE THAT CARRIES A FINDING. A lane whose source has PUBLISHED is running, so a
    detector that cannot state its lag has found a hop it cannot account for — the case this module
    exists for. Measured on the live estate 2026-09-11: of 267 declared cells, 252 have no visible
    source, and of the 15 that do, one destination cannot be read and one pair of stores disagrees.
    Folded into ``unmeasurable`` those two are one part in 252 and say nothing, which is
    indistinguishable from a healthy lane.
    """

    edges: int
    published_points: int
    failed: int
    unmeasurable: int = 0
    #: Every cell the detector owed a lag and could not give one, each carrying its reason. ONE list
    #: rather than a field per reason: both mean "this lane is running and its lag cannot be stated",
    #: and a field per state is the shape that let two sibling readers classify one identical refusal
    #: differently. Bounded — a cell reaches it only when the source answered with a published version,
    #: which on the live estate 2026-09-11 was 15 of 267 cells.
    blind: list[BlindEdge] = Field(default_factory=list)
    #: Cells NOT asked about this tick because :class:`AbsentEdgeMemo` has seen them refuse
    #: repeatedly. Counted rather than silent for the same reason `unmeasurable` is separate from
    #: `failed`: a detector that quietly stops asking looks exactly like one with nothing to report.
    skipped: int = 0


class AbsentEdgeMemo:
    """Remembers which (edge, project) cells keep answering "not visible", so they stop being ASKED.

    WHY ASKING COSTS ANYTHING. `declared_edges` is a cartesian product — every declared lane x every
    project in the warehouse registry — and most tenants do not run most lanes, so a large fraction of
    the cells name a table nobody created. Measured on the live estate 2026-09-08: 1,464 refusals to
    96 answers over thirty minutes, ~2,900 an hour. Each refusal is correct on both sides and each
    writes a `lance.audit` `access_denied` record, so the #41 compliance trail became mostly false
    denials and a real refusal is a needle in them.

    THE FAILURE THIS MUST NOT BECOME. This module's doctrine is that a cascade nobody measures is
    indistinguishable from a cascade with no lag. A memo that learns "absent" once and never looks
    again trades audit noise for silent blindness, which is strictly worse — so it is deliberately
    forgetful in two directions:

      * it takes ``MISSES_BEFORE_SKIP`` CONSECUTIVE refusals before skipping, so a tenant mid-
        onboarding or a catalog blip cannot cost a lane its series; and
      * it re-probes EVERYTHING every ``TICKS_BEFORE_REPROBE`` ticks, so a lane created after the memo
        learned about it is picked up rather than lost forever.

    Injected, never global: a detector's memory is state, and state a caller cannot see or reset is
    what makes a wrong one impossible to diagnose. `LagTickReport.skipped` reports its effect.
    """

    #: Consecutive refusals before a cell is skipped. One is not evidence; three across three ticks is.
    MISSES_BEFORE_SKIP = 3
    #: Ticks between full re-probes. Bounds how long a newly-created lane can go unmeasured.
    TICKS_BEFORE_REPROBE = 20

    def __init__(self) -> None:
        self._misses: dict[tuple[str, str], int] = {}
        self._ticks = 0

    def begin_tick(self) -> None:
        """Advance the tick counter, clearing the memo when a full re-probe is due."""
        self._ticks += 1
        if self._ticks >= self.TICKS_BEFORE_REPROBE:
            self.reset()

    def reset(self) -> None:
        """Forget everything — the next tick asks about every cell again."""
        self._misses.clear()
        self._ticks = 0

    def should_skip(self, cell: tuple[str, str]) -> bool:
        return self._misses.get(cell, 0) >= self.MISSES_BEFORE_SKIP

    def record_absent(self, cell: tuple[str, str]) -> None:
        self._misses[cell] = self._misses.get(cell, 0) + 1

    def record_present(self, cell: tuple[str, str]) -> None:
        """A cell that answered is back in the normal population immediately."""
        self._misses.pop(cell, None)


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
    memo: AbsentEdgeMemo | None = None,
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
    report = LagTickReport(edges=len(edges), published_points=0, failed=0)
    if memo is not None:
        memo.begin_tick()
    for edge, project in edges:
        cell = (edge, project)
        if memo is not None and memo.should_skip(cell):
            # NOT ASKED, and that is the whole change: the probe itself is what writes a false
            # `access_denied` into the compliance trail, so declining to issue it is the only thing
            # that removes the record. Counted, never silent — the memo re-probes on its own schedule.
            report.skipped += 1
            continue
        # THE TWO READS ARE SEPARATE CALLS, and that is the whole discriminator rather than a style
        # preference. Evaluated as one expression, a refusal from EITHER store yields one
        # `EdgeNotMeasurable` and the tick cannot tell "this project does not run this lane" from "this
        # lane is publishing into a destination I cannot read" — which is a lost hop.
        try:
            published_version = published(edge, project)
        except EdgeNotMeasurable:
            # The SOURCE is not visible, so this detector has no lane to measure. That is USUALLY a
            # project that does not run this lane — but not always, and the difference is invisible
            # here: an UNGOVERNED source table refuses identically. Measured 2026-09-11, eight declared
            # cells have an ungoverned source; three of those sources were dropped and three were never
            # written, which leaves TWO — `research-bronze$events` and `bind86-bronze$events`, both
            # actively compacted — that are running lanes this tick reads as lanes nobody runs. It
            # cannot be fixed from here, because neither door offers an existence oracle, so it is
            # fixed by governing the table and named here so the silence is not mistaken for a
            # measurement.
            #
            # No log line: this is a steady state, not an event, and one line per invisible edge per
            # tick is the shape that buried every other service's errors once already.
            report.unmeasurable += 1
            if memo is not None:
                memo.record_absent(cell)
            continue
        except Exception as exc:  # noqa: BLE001 — one edge's read must never end the tick
            log.warning("cascade_lag_edge_unreadable", extra={"edge": edge, "project": project, "error": str(exc)})
            report.failed += 1
            continue

        try:
            consumed_ranges = consumed(edge, project)
        except EdgeNotMeasurable:
            if published_version is None:
                # A source that exists and has never published has nothing to fall behind, so its
                # destination's absence is the expected shape of a lane nobody has run yet.
                report.unmeasurable += 1
                if memo is not None:
                    memo.record_absent(cell)
                continue
            # A PUBLISHED SOURCE AND AN UNREADABLE DESTINATION. The lane is running and the detector
            # cannot see where it lands — the loss this module exists for, and the one cell of 267 that
            # is not one of the 252 naming a lane their project does not run. It publishes NO lag: `require_metadata_access` runs before
            # existence resolution, so absent and forbidden answer alike, and this estate holds gold
            # tables that exist with zero tuples. Guessing would publish a confident level for a hop
            # that had in fact run.
            #
            # DELIBERATELY NOT MEMOIZED. `AbsentEdgeMemo` exists to stop probing cells that name
            # nothing, and each probe costs an `access_denied` audit record; this cell is the estate's
            # only evidence of that lost hop, so it pays one record a tick and stays in the population.
            report.blind.append(BlindEdge(edge=edge, project=project, reason=DESTINATION_INVISIBLE))
            log.warning("cascade_lag_edge_blind", extra={"edge": edge, "project": project, "reason": DESTINATION_INVISIBLE, "published": published_version})
            continue
        except Exception as exc:  # noqa: BLE001 — one edge's read must never end the tick
            log.warning("cascade_lag_edge_unreadable", extra={"edge": edge, "project": project, "error": str(exc)})
            report.failed += 1
            continue

        # BOTH STORES ANSWERED, so the cell is back in the normal population — recorded HERE rather
        # than after the lag is known, because answering is what the memo asks about and a disagreement
        # is still an answer. Left until later, a cell that had been invisible three times and then
        # became merely contradictory would stay skipped until the 20-tick re-probe: silent for exactly
        # the reason this list exists to prevent.
        if memo is not None:
            memo.record_present(cell)

        lag = lag_for_edge(edge=edge, project=project, published=published_version, consumed=consumed_ranges)
        if not lag.known:
            # BOTH STORES ANSWERED AND CONTRADICTED EACH OTHER, which is a finding rather than an
            # absence — and it is named here for the same reason the case above is. `record_edge_lag`
            # publishes nothing (no sentinel is safe), so the cell has no series, and a count in a log
            # line names no edge: an estate where every lane disagreed would publish nothing, page
            # nobody, and read exactly like a cascade with no lag.
            report.blind.append(BlindEdge(edge=edge, project=project, reason=STORES_DISAGREE))
            log.warning("cascade_lag_edge_blind", extra={"edge": edge, "project": project, "reason": STORES_DISAGREE, "published": published_version})
            continue
        record_edge_lag(lag, gauge=gauge)
        report.published_points += 1
    log.info(
        "cascade_lag_tick",
        extra={
            "edges": report.edges,
            "published": report.published_points,
            "failed": report.failed,
            "unmeasurable": report.unmeasurable,
            "skipped": report.skipped,
            # Per REASON, not a single total: the two are diagnosed and repaired differently, and a
            # summed "blind" count would hide one rising while the other fell.
            "destination_invisible": sum(1 for blind in report.blind if blind.reason == DESTINATION_INVISIBLE),
            "stores_disagree": sum(1 for blind in report.blind if blind.reason == STORES_DISAGREE),
        },
    )
    return report
