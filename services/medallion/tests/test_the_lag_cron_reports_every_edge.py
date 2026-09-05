"""One lag tick: read both stores per declared edge, publish what is known, stay silent on what is not.

docs/DECISIONS.md "Cascade repair" (C3), last piece. The arithmetic (`lag_for_edge`) and the recorder
(`record_edge_lag`) are pure; this is the tick that feeds them, and its whole job is to be honest about
partial failure.

READ FAILURES ARE PER EDGE, NEVER PER TICK. One unreadable table must not blank the estate: the other
edges' answers are still true, and a tick that abandoned them would turn a single bad table into an
estate-wide "no data" that reads exactly like a healthy idle cascade. This is the discipline
`maintenance/services/reconcile.py` already applies by keeping an unavailable CATEGORY out of its counts
while the categories that completed still report.

THE EVERY-REPLICA ANSWER IS CONVERGENCE, and it is recorded rather than deferred. `bindings.cron` fires
on every replica with no lease (`.claude/skills/rask-dapr`), and the estate answers that four ways. This
tick is read-only and idempotent: two replicas computing the same lag publish the same level, which is
what a gauge is for. No lock, no `replicas: 1` pin, no dedupe key — the same answer the catalog's
control relay takes for its own reason.
"""

from __future__ import annotations

from medallion.services.cascade_lag import ConsumedRange, EdgeNotMeasurable, LagTickReport, run_lag_tick


class _Gauge:
    def __init__(self) -> None:
        self.points: list[tuple[int, dict[str, str]]] = []

    def set(self, amount: int, /, attributes: dict[str, str] | None = None) -> None:
        self.points.append((amount, attributes or {}))


EDGES = [("bronze->silver", "acme"), ("silver->gold", "acme")]


def test_a_healthy_estate_publishes_a_point_per_edge() -> None:
    gauge = _Gauge()
    report = run_lag_tick(
        edges=EDGES,
        published=lambda edge, project: {"bronze->silver": 7, "silver->gold": 3}[edge],
        consumed=lambda edge, project: [ConsumedRange(from_version=None, to_version={"bronze->silver": 7, "silver->gold": 1}[edge])],
        gauge=gauge,
    )
    assert report == LagTickReport(edges=2, published_points=2, unknown=0, failed=0)
    assert sorted(v for v, _ in gauge.points) == [0, 2]


def test_one_unreadable_edge_does_not_blank_the_others() -> None:
    """The property that matters most. A raising reader is contained to ITS edge — the other edge's
    lag is still true, and abandoning it would turn one bad table into an estate-wide silence
    indistinguishable from a healthy idle cascade."""

    def _published(edge: str, project: str) -> int | None:
        if edge == "bronze->silver":
            raise RuntimeError("catalog unreachable for this table")
        return 3

    gauge = _Gauge()
    report = run_lag_tick(edges=EDGES, published=_published, consumed=lambda e, p: [ConsumedRange(from_version=None, to_version=1)], gauge=gauge)
    assert report.failed == 1
    assert report.published_points == 1
    assert gauge.points == [(2, {"lance.medallion.edge": "silver->gold", "lance.medallion.project": "acme"})]


def test_an_unknown_edge_is_counted_and_publishes_nothing() -> None:
    """Consumed ahead of published: the stores disagree. Counted so a tick that knows nothing is
    distinguishable from a tick that ran and found everything healthy."""
    gauge = _Gauge()
    report = run_lag_tick(edges=EDGES[:1], published=lambda e, p: 3, consumed=lambda e, p: [ConsumedRange(from_version=None, to_version=8)], gauge=gauge)
    assert report == LagTickReport(edges=1, published_points=0, unknown=1, failed=0)
    assert gauge.points == []


def test_a_tick_over_no_declared_edges_is_not_an_error() -> None:
    """A deployment with no lanes declared has nothing to measure. Zero is the honest report; raising
    would make an unconfigured estate look broken on every tick."""
    gauge = _Gauge()
    assert run_lag_tick(
        edges=[], published=lambda e, p: 1, consumed=lambda e, p: [ConsumedRange(from_version=None, to_version=1)], gauge=gauge
    ) == LagTickReport(edges=0, published_points=0, unknown=0, failed=0)


def _raise_unmeasurable(edge: str, project: str) -> int | None:
    raise EdgeNotMeasurable(f"{edge} is not visible to this subject")


def test_an_UNMEASURABLE_edge_is_counted_apart_from_a_failure() -> None:
    """A third outcome, because two were not enough to be honest.

    FAILED means "the store broke and the next tick may differ" — worth a warning and worth watching.
    UNMEASURABLE means "this subject cannot see that table, and asking again will not change that":
    the catalog gives one answer for "absent" and "forbidden", so an estate holding abandoned test
    projects reports hundreds of them. Counting those FAILED buries a real outage in permanent noise;
    counting them as a lag of 0 invents a healthy series for a cascade that does not exist.
    """
    gauge = _Gauge()
    report = run_lag_tick(
        edges=EDGES,
        published=_raise_unmeasurable,
        consumed=lambda e, p: [],
        gauge=gauge,
    )
    assert report.unmeasurable == len(EDGES)
    assert report.failed == 0
    assert report.published_points == 0
    assert gauge.points == []
