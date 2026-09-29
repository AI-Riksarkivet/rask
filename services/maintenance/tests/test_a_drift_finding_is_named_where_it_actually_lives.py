"""A named drift finding has to say WHERE it is, not just what it is called.

[[LH-094]]. `_drift_names` exists because "orphan_buckets: 12" gave an operator no way to learn which
twelve. `_finding_identity` takes the first present field from a fixed list — and that list reaches
`path` before it ever considers `dataset`, so an `OrphanFile` is named by a path RELATIVE to the
dataset it was found under. `OrphanFile.dataset` carries the dataset URI and its own comment says why
it exists: "so a finding is actionable without re-deriving it". The namer skipped the one field added
to make the finding actionable.

MEASURED ON THE LIVE ESTATE 2026-09-16, the first reconcile tick after the vend fix deployed:

    orphan_files: 932, across 13 buckets, named
      '_transactions/0-b2af7396-…​.txn', 'data/00110000000101010100111175e9d74acfb6cdbe0d7e0efc1f.lance', …

Thirteen buckets hold `data/` and `_transactions/`. The line reports 932 findings and the ten it names
identify none of them — the same failure `_drift_names` was written to end, reproduced one level down.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from maintenance.api.routes import _drift_names
from maintenance.services.orphans import OrphanFile
from maintenance.services.reconcile import OrphanedTrash, ReconcileReport, UnregisteredDataset


DATASET = "s3://lance-catalog/6ecbe11e_transcripts_v2$annotations"


@pytest.mark.parametrize(
    ("category", "finding", "name"),
    [
        pytest.param("orphan_files", OrphanFile(dataset=DATASET, path="data/x.lance", kind="data"), f"{DATASET}/data/x.lance", id="orphan-file"),
        # No root claims this table, so a table id (unique only WITHIN a root) cannot be its identity and
        # the URI is the only answer to "where do I go and look".
        pytest.param(
            "unregistered_datasets",
            UnregisteredDataset(table_id="ns$t", location="s3://lance-catalog/aa11bb22_ns$t"),
            "s3://lance-catalog/aa11bb22_ns$t",
            id="unregistered-dataset",
        ),
        # An orphaned trash record carries a `location` too and is named by its `id`: ranking `location`
        # ahead of `id` would silently rename an existing category's findings in the report an operator
        # reads every tick.
        pytest.param("orphaned_trash", OrphanedTrash(id="tr-1", kind="table", location="s3://gone/t.lance"), "tr-1", id="orphaned-trash"),
    ],
)
def test_a_drift_finding_is_named_by_where_it_lives(category: str, finding: BaseModel, name: str) -> None:
    report = ReconcileReport(checked_at="2026-09-16T13:50:21+00:00")
    setattr(report, category, [finding])

    named = _drift_names(report)

    assert named[category] == [name], named
