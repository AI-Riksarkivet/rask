"""Discovery lists a bucket's datasets for the orphan scan: a directory holding ``_versions/``, never the catalog's
``__manifest`` or a loose file. Infra-free (no S3, no Lance)."""

from __future__ import annotations

from typing import cast

import pyarrow.fs as pafs

from maintenance.services.discovery import discover_datasets


def _dir(path: str) -> pafs.FileInfo:
    return pafs.FileInfo(path, pafs.FileType.Directory)


class _FakeFS:
    """Path-aware fake covering the two calls discover_datasets makes: listing a prefix
    (FileSelector) and probing a single path (the ``_versions`` dataset marker)."""

    def __init__(self, tree: dict[str, list[pafs.FileInfo]]) -> None:
        self._tree = tree

    def get_file_info(self, selector: pafs.FileSelector | str) -> list[pafs.FileInfo] | pafs.FileInfo:
        if isinstance(selector, pafs.FileSelector):
            return self._tree.get(selector.base_dir, [])
        for infos in self._tree.values():
            for info in infos:
                if info.path == selector:
                    return info
        return pafs.FileInfo(selector, pafs.FileType.NotFound)


def test_discover_skips_manifest_and_non_dirs() -> None:
    fs = _FakeFS(
        {
            "lance-catalog": [
                _dir("lance-catalog/abcd_ns$table"),
                _dir("lance-catalog/__manifest"),  # bookkeeping → skip
                pafs.FileInfo("lance-catalog/loose.txt", pafs.FileType.File),  # not a dataset → skip
                _dir("lance-catalog/efgh_gold$catalog"),
            ],
            "lance-catalog/abcd_ns$table": [_dir("lance-catalog/abcd_ns$table/_versions")],
            "lance-catalog/efgh_gold$catalog": [_dir("lance-catalog/efgh_gold$catalog/_versions")],
        }
    )
    uris = discover_datasets(cast(pafs.FileSystem, fs), "lance-catalog").uris
    assert uris == ["s3://lance-catalog/abcd_ns$table", "s3://lance-catalog/efgh_gold$catalog"]
