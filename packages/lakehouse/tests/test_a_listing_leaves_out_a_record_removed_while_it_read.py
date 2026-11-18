"""A listing leaves out a record removed between its listing and its read.

A record a writer removed on its own decision (a conditional removal, `lakehouse.records.remove_json`) is not a record
the listing failed to read: it is no longer one, and a listing taken an instant later would not have named it. A strict
listing raises on a record it cannot read, because its caller decides on the whole listing or none, so treating a removed
record as unreadable would fail every reader that races a removal, with nothing wrong in the store.

Driven over a real local control root, with the removal placed between the listing and the read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lakehouse import record_store
from lakehouse.storage_options import StorageOptions


class _RemovesOnceListed:
    """The real filesystem, removing ``gone`` after the listing named it and before any record is read."""

    def __init__(self, fs: Any, gone: Path) -> None:
        self._fs, self._gone = fs, gone

    def get_file_info(self, selector: Any) -> Any:
        listed = self._fs.get_file_info(selector)
        self._gone.unlink()
        return listed

    def open_input_stream(self, path: str) -> Any:
        return self._fs.open_input_stream(path)


def test_a_strict_listing_leaves_out_a_record_removed_while_it_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = tmp_path / "_declared_grants"
    prefix.mkdir()
    (prefix / "kept.json").write_text(json.dumps({"grant": "kept"}))
    (prefix / "removed.json").write_text(json.dumps({"grant": "removed"}))
    real = record_store.fs_and_base

    def removing(control_root: str, storage_options: StorageOptions) -> tuple[Any, str]:
        fs, base = real(control_root, storage_options)
        return _RemovesOnceListed(fs, prefix / "removed.json"), base

    monkeypatch.setattr(record_store, "fs_and_base", removing)
    skipped: list[str] = []

    listed = record_store.list_records(str(tmp_path), StorageOptions(), "_declared_grants", event="declared_grant", strict=True, skipped=skipped)

    assert (listed, skipped) == ([{"grant": "kept"}], [])
