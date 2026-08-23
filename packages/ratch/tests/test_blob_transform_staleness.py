"""B4 on blob-bearing tables — rebuild granularity, because row granularity is not available.

A blob table's only legal write is the all-or-nothing `_rowid` rebuild: `merge_insert` crashes Lance's
blob decoder (invariant §7.1), which is why `run_scan_column_stage` refuses a partial NULL-fill there
outright. So the SELECTIVE re-run B4 gives non-blob tables cannot exist on these.

What can exist, and is the part that was missing, is knowing the column is STALE. Before this, a
fully-populated column was indistinguishable from a correct one: the driver dropped an all-NULL
leftover and rebuilt, but a column computed by a SUPERSEDED transform looked finished and was left
alone forever. Stamping `transform_version` during the rebuild makes "built by an older transform"
observable, and `blob_column_is_stale` is the decision that reads it.

The distinction this must not lose: a column with NO version (written before B4) is UNKNOWN, not
proven-stale. Rebuilding every such column on first upgrade would re-run every blob stage in the
estate at once, which is a far worse default than leaving them until something else touches them.
"""

from __future__ import annotations

from ratch.core.driver import blob_column_is_stale


def test_a_column_built_by_this_transform_is_not_stale() -> None:
    assert blob_column_is_stale({"abc123"}, identity="abc123") is False


def test_a_column_built_by_an_older_transform_is_stale() -> None:
    assert blob_column_is_stale({"old999"}, identity="abc123") is True


def test_an_unversioned_column_is_not_treated_as_stale() -> None:
    """Pre-B4 data. Unknown provenance is not evidence of staleness, and assuming it would rebuild
    every blob column in the estate the first time this ships."""
    assert blob_column_is_stale({None}, identity="abc123") is False
    assert blob_column_is_stale(set(), identity="abc123") is False


def test_a_mixed_column_is_stale() -> None:
    """More than one version present means some rows were computed by something else — and the only
    write available cannot fix a subset, so the whole column is what has to go."""
    assert blob_column_is_stale({"abc123", "old999"}, identity="abc123") is True
    assert blob_column_is_stale({"abc123", None}, identity="abc123") is True


def test_no_identity_means_no_opinion() -> None:
    """A caller that could not compute an identity must not cause a rebuild."""
    assert blob_column_is_stale({"old999"}, identity="") is False
