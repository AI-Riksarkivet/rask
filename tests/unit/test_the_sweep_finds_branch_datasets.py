"""A branch is a whole dataset under `tree/<name>/`, and the sweep never found one.

the lakehouse register, row C8 (drained 2026-09-10; in git history). `discover_datasets` treats a directory holding `_versions/` as a
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

from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
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


def test_every_branch_the_refs_name_is_discovered_as_its_own_dataset(tmp_path: Path) -> None:
    """The headline: a branch has its own `_versions/`, so it is a dataset the sweep must maintain.

    [[LH-203]] Named BY THE REFS. A name may contain `/`, so `a/b` sits inside `tree/a/` and `x/y` under a
    `tree/x/` that is no branch at all; a walk of `tree/`'s children found `tree/a` and missed both.
    Planted through pylance, which accepts both names (the catalog's create door now refuses `a/b`).
    """
    location = str(tmp_path / "bkt" / "t.lance")
    lance.write_dataset(pa.table({"id": [1, 2]}), location)
    for name in ("a", "a/b", "x/y"):
        lance.dataset(location).create_branch(name)
    fs = pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem())

    uris = discover_datasets(fs, "bkt").uris

    root = "s3://bkt/t.lance"
    assert sorted(uris) == [root, f"{root}/tree/a", f"{root}/tree/a/b", f"{root}/tree/x/y"]


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
