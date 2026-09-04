"""The per-edge lag reaches the metrics plane as a GAUGE, and an unknown edge publishes NOTHING.

`open_cascade_repair.md` C3. A lag is a LEVEL — true continuously, read by asking — so it is a gauge
set on each tick, never a counter and never a log line. Row 23 of `open_estate-verification.md` is what
the other choice costs: a repeating condition emitted per tick counted one gap 1210 times and buried
every other service's errors for ten hours.

SYNCHRONOUS `create_gauge`, not `observable_gauge`, and the difference is not stylistic: an observable
gauge computes inside an SDK callback, and this value comes from a catalog read and a lineage query.
IO in a collection callback blocks the exporter and fails invisibly. The cron tick already has a place
to do the reads; the gauge just records what it found.

AN UNKNOWN LAG PUBLISHES NO POINT AT ALL. That is the load-bearing rule and the reason this module
exists separately from the arithmetic: a gauge has no "unknown", so any sentinel becomes a number on a
dashboard. `-1` renders as a dip, `0` renders as perfect health — and `0` is what a HEALTHY edge
reports, so borrowing it turns an unreadable catalog into a clean bill of health. Publishing nothing
leaves the series STALE, which is what `for:` and a staleness alert are built to notice.
"""

from __future__ import annotations

from medallion.services.cascade_lag import EdgeLag, record_edge_lag


class _Gauge:
    def __init__(self) -> None:
        self.points: list[tuple[int, dict[str, str]]] = []

    def set(self, amount: int, /, attributes: dict[str, str] | None = None) -> None:
        self.points.append((amount, attributes or {}))


def test_a_known_lag_publishes_one_point_labelled_by_edge_and_project() -> None:
    gauge = _Gauge()
    record_edge_lag(EdgeLag(edge="bronze->silver", project="acme", lag=3, known=True), gauge=gauge)
    assert gauge.points == [(3, {"lance.medallion.edge": "bronze->silver", "lance.medallion.project": "acme"})]


def test_a_healthy_edge_publishes_a_real_zero() -> None:
    """Zero is a MEASUREMENT here and must be published: an edge that is level with its source is the
    normal state, and a missing point would make health indistinguishable from an unread store."""
    gauge = _Gauge()
    record_edge_lag(EdgeLag(edge="bronze->silver", project="acme", lag=0, known=True), gauge=gauge)
    assert gauge.points == [(0, {"lance.medallion.edge": "bronze->silver", "lance.medallion.project": "acme"})]


def test_an_unknown_lag_publishes_NOTHING() -> None:
    """No sentinel. A gauge has no "unknown", so every candidate value lies: -1 reads as a dip, 0 reads
    as perfect health. Silence leaves the series stale, which is a condition an alert can actually
    express."""
    gauge = _Gauge()
    record_edge_lag(EdgeLag(edge="bronze->silver", project="acme", lag=None, known=False), gauge=gauge)
    assert gauge.points == []


def test_the_attribute_values_are_bounded() -> None:
    """`edge` comes from the declared lane set and `project` from the registry — both bounded. Neither
    is caller-supplied, which is the rule that keeps this series from becoming unbounded."""
    gauge = _Gauge()
    for project in ("acme", "demo", "acme"):
        record_edge_lag(EdgeLag(edge="bronze->silver", project=project, lag=1, known=True), gauge=gauge)
    assert {tuple(sorted(attrs.items())) for _, attrs in gauge.points} == {
        (("lance.medallion.edge", "bronze->silver"), ("lance.medallion.project", "acme")),
        (("lance.medallion.edge", "bronze->silver"), ("lance.medallion.project", "demo")),
    }
