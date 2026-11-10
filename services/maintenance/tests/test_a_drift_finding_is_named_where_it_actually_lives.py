"""A named drift finding has to say WHERE it is, not just what it is called.

[[LH-094]]. `_drift_names` exists because "orphan_buckets: 12" gave an operator no way to learn which
twelve. `_finding_identity` takes the first present field from a fixed list, so the order of that list decides what
each category's findings are named by.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from maintenance.services.audit import _drift_names
from maintenance.services.reconcile import OrphanedTrash, ReconcileReport, UnregisteredDataset


@pytest.mark.parametrize(
    ("category", "finding", "name"),
    [
        # No `__manifest` row names this dataset, so its location is all there is to name it by ([[LH-520]]).
        pytest.param(
            "unregistered_datasets",
            UnregisteredDataset(location="s3://lance-catalog/0d9c6f3e-7b2a-4c1e-9f5d-2a8b3c4d5e6f"),
            "s3://lance-catalog/0d9c6f3e-7b2a-4c1e-9f5d-2a8b3c4d5e6f",
            id="unregistered-dataset",
        ),
        # An orphaned trash record carries a `location` too and is named by its `id`: ranking `location`
        # ahead of `id` would silently rename an existing category's findings in the report an operator
        # reads every tick.
        pytest.param(
            "orphaned_trash",
            OrphanedTrash(id="tr-1", uuid="5c1e8a3f-2d74-4b96-a0e5-7f9b3c6d2e81", kind="table", location="s3://gone/t.lance"),
            "tr-1",
            id="orphaned-trash",
        ),
    ],
)
def test_a_drift_finding_is_named_by_where_it_lives(category: str, finding: BaseModel, name: str) -> None:
    report = ReconcileReport(checked_at="2026-09-16T13:50:21+00:00")
    setattr(report, category, [finding])

    named = _drift_names(report)

    assert named[category] == [name], named
