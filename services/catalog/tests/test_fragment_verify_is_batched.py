"""Fragment data-file existence checks must be one batched lookup, not N serial HEADs (CAT-CORE-10).

`_verify_fragment_data_files` ran a nested `for frag ... for data_file ...` loop issuing one
`fs.get_file_info(single_path)` per data file — one object-store HEAD per file, serially, on the commit
hot path. pyarrow's list overload does the lookups concurrently, so all candidate paths go in one call.
"""

from __future__ import annotations

import pyarrow.fs as pafs
import pytest
from lance_namespace import InvalidInputError

from catalog.services import dataplane


class _CountingFS:
    """A fake filesystem recording how `get_file_info` is called and which paths report NotFound."""

    def __init__(self, missing: set[str]) -> None:
        self._missing = missing
        self.calls: list[str | list[str]] = []

    def get_file_info(self, paths: str | list[str]) -> pafs.FileInfo | list[pafs.FileInfo]:
        self.calls.append(paths)
        # Mirror pyarrow: a list argument returns a list of FileInfo, a single str returns one FileInfo.
        if isinstance(paths, list):
            return [self._info(p) for p in paths]
        return self._info(paths)

    def _info(self, path: str) -> pafs.FileInfo:
        kind = pafs.FileType.NotFound if path in self._missing else pafs.FileType.File
        return pafs.FileInfo(path, kind)


def _fragments(names: list[str]) -> list[dict[str, object]]:
    return [{"files": [{"path": n, "base_id": None}]} for n in names]


def test_all_data_files_are_verified_in_one_batched_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fs = _CountingFS(missing=set())
    monkeypatch.setattr(dataplane, "_dataset_fs", lambda location, so: (fs, "root"))

    dataplane._verify_fragment_data_files("s3://b/t", {}, _fragments(["a.lance", "b.lance", "c.lance"]))

    # One batched lookup covering all three files — not three serial single-path HEADs.
    assert len(fs.calls) == 1, f"expected one batched get_file_info, got {len(fs.calls)} serial calls"
    assert isinstance(fs.calls[0], list)
    assert len(fs.calls[0]) == 3


def test_a_missing_file_is_still_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    fs = _CountingFS(missing={"root/data/b.lance"})
    monkeypatch.setattr(dataplane, "_dataset_fs", lambda location, so: (fs, "root"))

    with pytest.raises(InvalidInputError, match="b.lance"):
        dataplane._verify_fragment_data_files("s3://b/t", {}, _fragments(["a.lance", "b.lance"]))
