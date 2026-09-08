"""An edge that is not measurable must stop being ASKED every tick — without ever going silent.

MEASURED ON THE LIVE ESTATE 2026-09-08 (§ H15). `declared_edges` is a cartesian product — every
declared lane x every project in the warehouse registry, 3 x 93 on this estate — and most tenants do
not run most lanes, so roughly two thirds of the cells name a table nobody created. Over 30 minutes
the catalog answered:

    GET /v1/table/<ns>$features/tags/list   1464 x 403   96 x 200   6 x 404

Neither side is wrong. The reader treats 403/404 as one answer and excludes the edge rather than
fabricating a level; the catalog answers 403 for an object with no tuples because existence is not an
oracle. The cost lands somewhere neither of them looks: every refusal writes a `lance.audit`
`access_denied` record, so the #41 compliance trail became ~2,900 false denials an hour and a real
refusal is a needle in them.

THE DANGEROUS FIX IS THE OBVIOUS ONE. This module's whole doctrine is that "a cascade nobody measures
is indistinguishable from a cascade with no lag" — so a memo that remembers an edge as absent and
never looks again would trade an audit-noise problem for a silent-blindness one, which is strictly
worse. The memo therefore has three properties, and each is pinned below: it takes REPEATED evidence
before skipping, it RE-PROBES everything periodically, and the skips are COUNTED in the report so the
state is never invisible.
"""

from __future__ import annotations

from medallion.services.cascade_lag import AbsentEdgeMemo, EdgeNotMeasurable, LagTickReport, run_lag_tick


EDGES = [("bronze->silver", "tenant-a"), ("bronze->silver", "tenant-b")]


class _Gauge:
    """POSITIONAL-ONLY `value`, matching `LagGauge` exactly — its own comment explains why: OTel's
    instrument names that parameter `amount`, so a by-name double conforms to the protocol and the real
    instrument does not, which is the shape where the suite is green and the deployed call raises."""

    def set(self, value: int, /, attributes: dict[str, str] | None = None) -> None:
        return None


def _tick(memo: AbsentEdgeMemo, *, absent: set[tuple[str, str]]) -> tuple[LagTickReport, list[tuple[str, str]]]:
    """One tick where every edge in `absent` is unmeasurable. Returns the report and what was ASKED."""
    asked: list[tuple[str, str]] = []

    def published(edge: str, project: str) -> int | None:
        asked.append((edge, project))
        if (edge, project) in absent:
            raise EdgeNotMeasurable(f"{edge}/{project} is not visible to this subject")
        return 5

    return run_lag_tick(edges=EDGES, published=published, consumed=lambda e, p: [], gauge=_Gauge(), memo=memo), asked


def test_an_absent_edge_is_asked_about_REPEATEDLY_before_it_is_skipped() -> None:
    """One miss is not evidence. A tenant mid-onboarding, or a catalog blip, must not cost the estate
    a lane's series — so the memo needs the answer to be stable before it acts on it."""
    memo = AbsentEdgeMemo()
    absent = {("bronze->silver", "tenant-b")}
    for tick in range(AbsentEdgeMemo.MISSES_BEFORE_SKIP):
        _, asked = _tick(memo, absent=absent)
        assert ("bronze->silver", "tenant-b") in asked, f"tick {tick}: the edge was skipped on too little evidence"


def test_a_STABLY_absent_edge_stops_being_asked() -> None:
    """The point of the change: the probe that produces the false audit record stops being issued."""
    memo = AbsentEdgeMemo()
    absent = {("bronze->silver", "tenant-b")}
    for _ in range(AbsentEdgeMemo.MISSES_BEFORE_SKIP):
        _tick(memo, absent=absent)
    report, asked = _tick(memo, absent=absent)
    assert ("bronze->silver", "tenant-b") not in asked, "a stably-absent edge is still being asked about every tick"
    assert ("bronze->silver", "tenant-a") in asked, "a measurable edge was skipped — the memo is over-reaching"
    assert report.skipped == 1, f"the tick skipped an edge and reported {report.skipped}"


def test_the_memo_RE_PROBES_so_a_new_lane_is_never_missed_forever() -> None:
    """The safety property that makes this safe to land at all.

    A tenant that creates its silver lane after the memo learned it was absent must be picked up. A
    memo without this is the silent-blindness failure the module exists to avoid — worse than the
    audit noise it removes.
    """
    memo = AbsentEdgeMemo()
    absent = {("bronze->silver", "tenant-b")}
    for _ in range(AbsentEdgeMemo.MISSES_BEFORE_SKIP):
        _tick(memo, absent=absent)

    asked_since: list[tuple[str, str]] = []
    for _ in range(AbsentEdgeMemo.TICKS_BEFORE_REPROBE + 1):
        _, asked = _tick(memo, absent=absent)
        asked_since.extend(asked)
    assert ("bronze->silver", "tenant-b") in asked_since, (
        f"the edge was not re-probed within {AbsentEdgeMemo.TICKS_BEFORE_REPROBE} ticks — a lane created later would never be measured"
    )


def test_an_edge_that_becomes_measurable_is_counted_again() -> None:
    """The other direction: once it answers, it must return to the normal population immediately."""
    memo = AbsentEdgeMemo()
    absent = {("bronze->silver", "tenant-b")}
    for _ in range(AbsentEdgeMemo.MISSES_BEFORE_SKIP):
        _tick(memo, absent=absent)
    memo.reset()  # the periodic re-probe, forced
    report, asked = _tick(memo, absent=set())
    assert ("bronze->silver", "tenant-b") in asked
    assert report.skipped == 0, "a measurable edge was still counted as skipped"
    assert report.published_points == 2, f"both edges should publish once visible, got {report.published_points}"


def test_no_memo_is_the_previous_behaviour_exactly() -> None:
    """The memo is injected, so a caller that passes none asks about everything — which is what every
    existing caller and test does."""
    report, asked = _tick(AbsentEdgeMemo(), absent={("bronze->silver", "tenant-b")})
    assert len(asked) == 2
    assert report.unmeasurable == 1
    assert report.skipped == 0

    plain = run_lag_tick(edges=EDGES, published=lambda e, p: 5, consumed=lambda e, p: [], gauge=_Gauge())
    assert plain.skipped == 0, "a tick with no memo reported a skip"
