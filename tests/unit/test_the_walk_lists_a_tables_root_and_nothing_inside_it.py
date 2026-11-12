"""The storage walk lists each dataset's root and nothing inside it, its branches included.

A branch is part of its table: the table's pass lists its branches through Lance's own ``branches.list()`` and
maintains each (``table_pass._datasets_of``), and the drift report judges a table by its root. So the walk reads no
ref file of Lance's, and a branch adds no finding of its own: a live table's branch is named by its table, and a
recoverably dropped table's branch lies under the trash record that keeps the table's bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pyarrow.fs as pafs

from maintenance.services.discovery import discover_datasets


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


def test_the_walk_lists_a_tables_root_and_none_of_its_branches(tmp_path: Path) -> None:
    """A branch is part of its table, so the walk lists the table's root alone: a recoverably dropped table's branch,
    whose bytes the table's trash record keeps, is then no dataset the catalog does not know."""
    location = tmp_path / "bkt" / "t.lance"
    lance.write_dataset(pa.table({"id": [1, 2]}), str(location))
    for name in ("a", "a/b"):
        lance.dataset(str(location)).create_branch(name)
    fs = pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem())

    discovered = discover_datasets(fs, "bkt").uris

    assert discovered == ["s3://bkt/t.lance"]


def test_a_datasets_own_internals_are_NOT_walked() -> None:
    """The bound that keeps this from becoming a full recursive crawl.

    A dataset holds `data/`, `_versions/`, `_transactions/`, `_indices/`, `_deletions/` — none is a
    dataset, and probing each for a `_versions/` marker is a wasted S3 round trip per directory per
    dataset on the hot discovery path. Nothing inside a dataset is descended into.
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
