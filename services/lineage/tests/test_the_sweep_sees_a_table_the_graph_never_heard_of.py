"""The sweep announces a GOVERNED table it holds no dataset node for at all.

[[LH-004]]. `docs/DECISIONS.md:766-812` accepts the Lance-commit -> lineage-publish window in writing,
and the acceptance rests on one clause: *"the goal is not atomicity; it is NO SILENT LOSS: every gap
either closes itself or announces itself"*. The same entry states the gap this leaves — *"`stage_event`
runs AFTER the Lance commit, so a crash in the commit->stage gap still loses the event"*.

FOR A DATASET'S FIRST WRITE, NOTHING ANNOUNCED IT. `reconcile_all` enumerates
`repository.list_datasets()` — the GRAPH — so a table whose first write lost its event has no node to
enumerate and is invisible to every axis the sweep has. `versions_without_lineage` finds holes BELOW a
known dataset's tip; it cannot find a dataset. The sweep already loads the governed-table set
(`governed_tables`, one enumeration per tick) and used it in ONE direction only: to mark a dataset
already in the graph as UNGOVERNED. The other direction is a set difference over data already in hand.

MEASURED ON THE DEPLOYED ESTATE 2026-09-19, before this existed: 1,424 governed tables, 1,297 graph
datasets, **127 governed tables with no node in the graph** — and 0 graph datasets that were not
governed, so the blindness was entirely one-sided. That 127 is the size of the blind spot, not a count
of lost writes: some of those tables were created and never written. Which is the point. Nothing could
tell the two apart, because nothing looked.

NOT AUTO-FIXED, deliberately, unlike `versions_without_lineage`. Back-filling a hole below a known tip
stamps a version onto a dataset whose storage URI the graph already holds; a table the graph has never
seen has no `dataSource` to read, so a node invented for it would assert a write nobody observed.
"""

from __future__ import annotations

import logging

import pytest

from lineage.api.reconcile_cron import log_sweep, record_sweep, summarize_sweep
from lineage.core import metrics as lineage_metrics
from lineage.schemas import ReconcileState, ReconcileStatus


def _known(name: str) -> ReconcileStatus:
    return ReconcileStatus(dataset=name, graph_version=3, storage_version=3, in_sync=True, status=ReconcileState.IN_SYNC)


_STATUSES = [_known("acme-bronze$events"), _known("acme-gold$catalog")]


def test_a_governed_table_with_no_graph_node_is_reported() -> None:
    report = summarize_sweep(_STATUSES, governed={"acme-bronze$events", "acme-gold$catalog", "models$e2etrain16211"})

    assert report.unknown_to_graph == ["models$e2etrain16211"]


def test_a_graph_that_covers_every_governed_table_reports_nothing() -> None:
    """The control. Without it, a field that always listed the governed set would pass above."""
    report = summarize_sweep(_STATUSES, governed={"acme-bronze$events", "acme-gold$catalog"})

    assert report.unknown_to_graph == []


def test_an_unasked_governed_question_condemns_nothing() -> None:
    """``None`` means FGA was off or the store was unreadable — the same load-bearing case as UNGOVERNED.

    An empty set would report every dataset in the estate as invisible at exactly the moment the sweep
    lost its ability to ask, which is the failure `governed_tables` returns an optional to avoid.
    """
    assert summarize_sweep(_STATUSES, governed=None).unknown_to_graph is None


def test_an_unasked_question_is_not_a_clean_bill_of_health() -> None:
    """``None`` and ``[]`` must stay distinguishable, because the gauge publishes a point for one only.

    Collapsed to ``[]``, a sweep that could not reach FGA reports the same zero a healthy estate does —
    and zero is the number an operator trusts. `record_provenance_gaps` publishes NO point for ``None``
    so the series goes stale instead.
    """
    asked = summarize_sweep(_STATUSES, governed={s.dataset for s in _STATUSES})

    assert asked.unknown_to_graph == []
    assert summarize_sweep(_STATUSES, governed=None).unknown_to_graph is not asked.unknown_to_graph


def test_the_finding_is_sorted_so_two_ticks_are_comparable() -> None:
    """A set iterates in hash order, so an unsorted field makes every tick's log line look like a change."""
    governed = {"z$last", "a$first", "m$middle", *{s.dataset for s in _STATUSES}}

    assert summarize_sweep(_STATUSES, governed=governed).unknown_to_graph == ["a$first", "m$middle", "z$last"]


def test_the_class_gets_its_own_warning(caplog: pytest.LogCaptureFixture) -> None:
    """One body per finding class, like every sibling in `log_sweep` — an operator filters on it."""
    report = summarize_sweep(_STATUSES, governed={"models$e2etrain16211", *{s.dataset for s in _STATUSES}})
    with caplog.at_level(logging.WARNING):
        log_sweep(report)

    warned = [r for r in caplog.records if r.message == "lineage_reconcile_unknown_to_graph"]
    assert len(warned) == 1
    assert getattr(warned[0], "count", None) == 1


def test_a_clean_sweep_warns_about_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        log_sweep(summarize_sweep(_STATUSES, governed={s.dataset for s in _STATUSES}))

    assert [r for r in caplog.records if r.message == "lineage_reconcile_unknown_to_graph"] == []


class _Gauge:
    """Records what was published. Signature matches OTel's ``Gauge.set`` — value positional-only."""

    def __init__(self) -> None:
        self.points: list[tuple[int, str]] = []

    def set(self, value: int, /, attributes: dict[str, str] | None = None) -> None:
        self.points.append((value, (attributes or {}).get("lance.lineage.provenance_gap", "")))


@pytest.fixture
def gauge(monkeypatch: pytest.MonkeyPatch) -> _Gauge:
    spy = _Gauge()
    monkeypatch.setattr(lineage_metrics, "_provenance_missing", spy)
    return spy


def test_the_count_reaches_a_series_an_alert_can_read(gauge: _Gauge) -> None:
    """A WARN cannot page: `chart/alerting/rules.yml` evaluates series, not log bodies."""
    record_sweep(summarize_sweep(_STATUSES, governed={"models$e2etrain16211", *{s.dataset for s in _STATUSES}}))

    assert (1, "unknown_to_graph") in gauge.points


def test_a_blind_sweep_publishes_NO_point_rather_than_zero(gauge: _Gauge) -> None:
    """The load-bearing rule. Zero is what a healthy estate reports, so a blind sweep must not report it.

    With no point the series goes STALE, which a staleness alert notices; with a zero it reads as a
    clean bill of health at exactly the moment the sweep lost the ability to look.
    """
    record_sweep(summarize_sweep(_STATUSES, governed=None))

    assert [p for p in gauge.points if p[1] == "unknown_to_graph"] == []


def test_the_sibling_class_still_publishes_when_the_first_is_blind(gauge: _Gauge) -> None:
    """The control: `versions_below_tip` is read off the statuses and does not depend on FGA at all."""
    record_sweep(summarize_sweep(_STATUSES, governed=None))

    assert [p for p in gauge.points if p[1] == "versions_below_tip"] == [(0, "versions_below_tip")]


def test_versions_are_counted_not_datasets(gauge: _Gauge) -> None:
    """One dataset missing three versions is three lost writes, not one."""
    holed = ReconcileStatus(
        dataset="acme-bronze$events", graph_version=9, storage_version=9, in_sync=True, status=ReconcileState.IN_SYNC, versions_without_lineage=[4, 6, 7]
    )
    record_sweep(summarize_sweep([holed], governed=None))

    assert (3, "versions_below_tip") in gauge.points
