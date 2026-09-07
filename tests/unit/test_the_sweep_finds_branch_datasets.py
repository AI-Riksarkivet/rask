"""A branch is a whole dataset under `tree/<name>/`, and the sweep never found one.

`open_lakehouse_diff_left.md` § C8. `discover_datasets` treats a directory holding `_versions/` as a
dataset and stops there — it does not recurse into what a dataset CONTAINS. A Lance branch lives at
`<dataset>/tree/<branch>/` with its own `_versions/` and `_transactions/`, so every branch in the
estate was invisible to the sweep: never version-cleaned, never index-optimized, growing without bound.

MEASURED ON THE DEPLOYED ESTATE 2026-09-07: **85 of 250 tables carry at least one branch** — a third of
the catalog. This is a live leak, not a hypothetical.

WHAT A BRANCH IS, measured on pylance 10.0.0 rather than assumed, because it decides what maintenance
is safe on one::

    branch feature flags   (16, 16)     FLAG_BASE_PATHS
    branch data files      identical to main's, base_id = 0
    tree/<name>/ holds     _versions, _transactions — and NO data/ of its own

So a branch is exactly the shallow-clone shape: its files resolve through `base_paths` to the parent's
root. That is why this change is DISCOVERY ONLY. The three existing gates already give the right
answer for flag 16 and none of them is touched:

* **compaction REFUSES it** — rewriting a clone materialises the parent's data into it, the measured
  storage-amplification hazard `SUPPORTED` exists to prevent;
* **the orphan scan REFUSES it** — "list the prefix, subtract what is referenced" would report the
  parent's live files as garbage;
* **root-scoped GC PERMITS it** (`SUPPORTED_FOR_GC`) — `cleanup_old_versions` and `optimize_indices`
  reclaim only what lives under the dataset's own root, which the estate measured on real clones.

So finding branches buys exactly the maintenance that is safe on them, and the safety is enforced where
it already was rather than by a new rule here.
"""

from __future__ import annotations

from typing import Any, cast

import pyarrow.fs as pafs

from maintenance.services.optimize import discover_datasets


def _dir(path: str) -> pafs.FileInfo:
    return pafs.FileInfo(path, pafs.FileType.Directory)


class _FakeFS:
    """Path-aware fake covering the two calls discover_datasets makes: listing a prefix and probing a
    single path for the ``_versions`` marker."""

    def __init__(self, tree: dict[str, list[pafs.FileInfo]]) -> None:
        self._tree = tree

    def get_file_info(self, selector: Any) -> Any:
        if isinstance(selector, pafs.FileSelector):
            return self._tree.get(selector.base_dir, [])
        for infos in self._tree.values():
            for info in infos:
                if info.path == selector:
                    return info
        return pafs.FileInfo(selector, pafs.FileType.NotFound)


def _estate() -> _FakeFS:
    """One table with two branches, plus a plain table — the shape 85 live tables have."""
    return _FakeFS(
        {
            "lance-catalog": [_dir("lance-catalog/abcd_ns$t1"), _dir("lance-catalog/efgh_ns$t2")],
            "lance-catalog/abcd_ns$t1": [
                _dir("lance-catalog/abcd_ns$t1/_versions"),
                _dir("lance-catalog/abcd_ns$t1/_transactions"),
                _dir("lance-catalog/abcd_ns$t1/data"),
                _dir("lance-catalog/abcd_ns$t1/tree"),
            ],
            "lance-catalog/abcd_ns$t1/tree": [
                _dir("lance-catalog/abcd_ns$t1/tree/work"),
                _dir("lance-catalog/abcd_ns$t1/tree/experiment"),
            ],
            "lance-catalog/abcd_ns$t1/tree/work": [_dir("lance-catalog/abcd_ns$t1/tree/work/_versions")],
            "lance-catalog/abcd_ns$t1/tree/experiment": [_dir("lance-catalog/abcd_ns$t1/tree/experiment/_versions")],
            "lance-catalog/efgh_ns$t2": [_dir("lance-catalog/efgh_ns$t2/_versions")],
        }
    )


def test_a_branch_under_tree_is_discovered_as_its_own_dataset() -> None:
    """The headline: a branch has its own `_versions/`, so it is a dataset the sweep must maintain."""
    uris = discover_datasets(cast(Any, _estate()), "lance-catalog").uris

    assert "s3://lance-catalog/abcd_ns$t1/tree/work" in uris, (
        "the sweep did not find a branch — every branch in the estate is unmaintained, its versions growing without bound"
    )
    assert "s3://lance-catalog/abcd_ns$t1/tree/experiment" in uris, "a SECOND branch on the same table was missed"


def test_the_parent_is_still_discovered_and_reported_once() -> None:
    """Finding branches must not cost the parent, nor duplicate it."""
    uris = discover_datasets(cast(Any, _estate()), "lance-catalog").uris

    assert uris.count("s3://lance-catalog/abcd_ns$t1") == 1, f"the parent was lost or duplicated: {uris}"
    assert "s3://lance-catalog/efgh_ns$t2" in uris, "a branchless table stopped being discovered"


def test_the_tree_PREFIX_is_never_itself_reported_as_a_dataset() -> None:
    """`tree/` is a container, like a namespace prefix — it holds no `_versions/` of its own.

    Reporting it would make the sweep open a path that is not a dataset and file the failure as an
    error, which is the noise the namespace-prefix recursion already exists to avoid.
    """
    uris = discover_datasets(cast(Any, _estate()), "lance-catalog").uris
    assert "s3://lance-catalog/abcd_ns$t1/tree" not in uris, "the tree/ container was reported as a dataset"


def test_a_datasets_own_internals_are_NOT_walked() -> None:
    """The bound that keeps this from becoming a full recursive crawl.

    A dataset holds `data/`, `_versions/`, `_transactions/`, `_indices/`, `_deletions/` — none is a
    dataset, and probing each for a `_versions/` marker is a wasted S3 round trip per directory per
    dataset on the hot discovery path. Only `tree/` is descended into.
    """
    fs = _FakeFS(
        {
            "lance-catalog": [_dir("lance-catalog/abcd_ns$t1")],
            "lance-catalog/abcd_ns$t1": [
                _dir("lance-catalog/abcd_ns$t1/_versions"),
                _dir("lance-catalog/abcd_ns$t1/data"),
                _dir("lance-catalog/abcd_ns$t1/_indices"),
            ],
            # A booby trap: if the walk descended blindly it would find this and call it a dataset.
            "lance-catalog/abcd_ns$t1/data": [_dir("lance-catalog/abcd_ns$t1/data/_versions")],
        }
    )
    uris = discover_datasets(cast(Any, fs), "lance-catalog").uris
    assert uris == ["s3://lance-catalog/abcd_ns$t1"], f"the walk descended into a dataset's own internals: {uris}"
