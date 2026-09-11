"""`graph_ahead` is a readable dataset at an older version; reporting it as loss cries wolf.

`summarize_sweep` put `GRAPH_AHEAD` and `MISSING_ON_STORAGE` in one `storage_loss` list. They are not
the same finding:

* MISSING_ON_STORAGE — the dataset could not be found at all. Data the graph records is gone.
* GRAPH_AHEAD — the dataset was READ successfully and sits at a LOWER version than the graph records.

MEASURED ON THE LIVE ESTATE 2026-09-11: the sweep warns `storage_loss=32` every 300 s tick, stable
across every tick sampled, and LH-002's own re-measurement found **29 of those 32 are live,
catalog-registered, readable tables** — the catalog describes them 200 with a location and `count_rows`
answers 3. They are e2e residue by name (`probe$nonexistent`, `e2e-ns$t178b2dda`, `tracka$…`), and an
e2e run that drops and recreates a table leaves it at v1 while the graph still holds v3. That is
GRAPH_AHEAD, it is expected, and under one name it is indistinguishable from destruction.

WHAT IS NOT CHANGING, because it was a deliberate decision rather than an accident: neither state is
back-fillable. `BACKFILLABLE_STATES` stays disjoint from both, the sweep still auto-fixes neither, and
both still WARN. The grouping's own test pinned exactly that disjointness — the reporting NAME rode
along with it, and only the name is wrong.

WHY A NAME IS WORTH A CHANGE. An alarm that is ~91% benign by its own row's count is one an operator
learns to skim, which is the failure mode this module guards against everywhere else — the truncation
warning exists so "a smaller finding next tick does not read as progress nobody made", and the
absent-vs-unreadable split exists because "reporting 'we could not open it' as 'it was destroyed' is a
false alarm an operator acts on". This is that same rule applied one state further along.
"""

from __future__ import annotations

from lineage.api import reconcile_cron
from lineage.core.reconcile import BACKFILLABLE_STATES
from lineage.schemas import ReconcileState, ReconcileStatus


def _statuses() -> list[ReconcileStatus]:
    return [
        ReconcileStatus(dataset="recreated", in_sync=False, status=ReconcileState.GRAPH_AHEAD),
        ReconcileStatus(dataset="gone", in_sync=False, status=ReconcileState.MISSING_ON_STORAGE),
        ReconcileStatus(dataset="fine", in_sync=True, status=ReconcileState.IN_SYNC),
    ]


def test_a_graph_ahead_dataset_is_not_called_storage_loss() -> None:
    """THE GATE. A readable table at an older version is not data the estate lost."""
    report = reconcile_cron.summarize_sweep(_statuses())

    assert report.storage_loss == ["gone"], "only a dataset that could not be found at all is loss"
    assert report.graph_ahead == ["recreated"], "the graph being ahead is its own finding and must be reported as one"


def test_both_states_are_still_reported_and_neither_is_auto_fixed() -> None:
    """The half that must not regress: splitting the NAME must not silence either finding.

    The previous grouping's own test pinned that these two are disjoint from `BACKFILLABLE_STATES`, and
    that decision is untouched — the sweep still recreates no data and still warns on both.
    """
    report = reconcile_cron.summarize_sweep(_statuses())

    assert report.storage_loss and report.graph_ahead, "splitting must not drop either class"
    assert ReconcileState.GRAPH_AHEAD not in BACKFILLABLE_STATES, "the graph being ahead is still not a lost write"
    assert ReconcileState.MISSING_ON_STORAGE not in BACKFILLABLE_STATES, "lost data is still not recreatable"
    assert report.backfilled == [], "neither state is auto-fixed"


def test_the_two_findings_get_their_own_warn_lines(caplog) -> None:  # noqa: ANN001 — pytest fixture
    """An operator greps a body; one name for two findings makes the benign one hide the real one."""
    caplog.set_level("WARNING")
    reconcile_cron.log_sweep(reconcile_cron.summarize_sweep(_statuses()))

    bodies = {record.message for record in caplog.records}
    assert "lineage_reconcile_storage_loss" in bodies, "real loss keeps the name an operator already alerts on"
    assert "lineage_reconcile_graph_ahead" in bodies, "the benign class needs a body of its own to be filtered separately"
