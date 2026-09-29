"""The reconcile sweep's SHAPE: one round-trip depth per dataset, and a cron body small enough to read.

Two properties `tests/unit/test_reconcile.py` cannot see, because it drives the sweep end to end and only
looks at what comes out:

* :func:`lineage.core.reconcile.reconcile_all` issues its three independent per-dataset graph lookups
  TOGETHER. Awaited one after another they made every dataset three round-trips deep, so a sweep over an
  estate of N datasets paid 3N serial round-trips to the same graph.
* the cron tick's partitioning of a sweep result is a NAMED, pure function. It used to be inlined in
  ``_on_cron`` — five comprehensions, five WARN branches and two literal 9-key mappings in one 115-line
  body — so the only way to exercise a partition was to run a whole sweep.
"""

from __future__ import annotations

from lineage.api import reconcile_cron
from lineage.schemas import ReconcileState, ReconcileStatus


def test_the_cron_partitions_a_sweep_through_a_named_pure_function() -> None:
    """The tick's report is derived from a list of statuses and nothing else, so it belongs in a function
    a test can call with a handful of statuses — not inlined in the request handler, where the only way
    to reach a partition is to drive a whole sweep against a repository double."""
    statuses = [
        ReconcileStatus(dataset="ahead", in_sync=False, status=ReconcileState.STORAGE_AHEAD),
        ReconcileStatus(dataset="lost", in_sync=False, status=ReconcileState.MISSING_ON_STORAGE),
        ReconcileStatus(dataset="blind", in_sync=False, status=ReconcileState.UNREADABLE, unreadable_reason="no creds"),
        ReconcileStatus(dataset="rotten", in_sync=True, status=ReconcileState.IN_SYNC, dangling_blob_columns=["payload"]),
        ReconcileStatus(dataset="old", in_sync=True, status=ReconcileState.IN_SYNC, stale=True),
        ReconcileStatus(dataset="thin", in_sync=True, status=ReconcileState.IN_SYNC, missing_declared_columns=["id"]),
    ]

    report = reconcile_cron.summarize_sweep(statuses)

    assert report.checked == 6
    assert report.backfilled == ["ahead"]
    assert report.storage_loss == ["lost"]
    assert report.unreadable == {"blind": "no creds"}
    assert report.dangling_blobs == {"rotten": ["payload"]}
    assert report.stale == ["old"]
    assert report.contract_violations == {"thin": ["id"]}
