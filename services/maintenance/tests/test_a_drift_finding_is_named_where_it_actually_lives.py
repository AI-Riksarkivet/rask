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

from maintenance.api.routes import _drift_names, _finding_identity
from maintenance.services.orphans import OrphanFile
from maintenance.services.reconcile import ReconcileReport


DATASET = "s3://lance-catalog/6ecbe11e_transcripts_v2$annotations"


def test_an_orphan_file_is_named_by_the_dataset_it_was_found_under() -> None:
    identity = _finding_identity(OrphanFile(dataset=DATASET, path="data/00110000.lance", kind="data", size_bytes=17))

    assert DATASET in identity, f"{identity!r} does not say which dataset holds this file, so nobody can go and look at it"
    assert "data/00110000.lance" in identity, f"{identity!r} lost the file, so the finding names a dataset instead of an orphan"


def test_two_orphans_with_the_same_relative_path_are_distinguishable() -> None:
    """The failure in its sharpest form: every Lance dataset has a `data/` and a `_transactions/`, so
    an unqualified path is not even unique across the report."""
    a = _finding_identity(OrphanFile(dataset="s3://bucket-a/t", path="_transactions/0-x.txn", kind="transactions"))
    b = _finding_identity(OrphanFile(dataset="s3://bucket-b/t", path="_transactions/0-x.txn", kind="transactions"))

    assert a != b, f"two orphans in different buckets are both named {a!r}"


def test_a_finding_with_no_dataset_is_still_named() -> None:
    """The fallback must not regress: a category whose model carries no `dataset` keeps its own name."""
    report = ReconcileReport(checked_at="2026-09-16T13:50:21+00:00")
    report.orphan_files = [OrphanFile(dataset=DATASET, path="data/x.lance", kind="data")]

    named = _drift_names(report)

    assert named["orphan_files"] == [f"{DATASET}/data/x.lance"], named
