"""The base-path presence gate must not walk the referenced set one round trip at a time (MAINT-12).

`_unscannable_reason` asks "does every referenced file live under this prefix". It asked with one
`get_file_info(path)` per referenced path, in a comprehension, sequentially — and this runs on EVERY
dataset in EVERY warehouse bucket on every reconcile tick, before a single orphan has been found. A
dataset's referenced set is one entry per data file, per deletion file, per index and per transaction
across every live version, so the cost is a per-dataset burst of HEADs proportional to its history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pyarrow.fs as pafs

from maintenance.services.orphans import scan_dataset


class _CountingFs(pafs.LocalFileSystem):
    """Records how the presence probes were issued: one call per path, or one call for the batch."""

    def __init__(self) -> None:
        super().__init__()
        self.single_path_calls: list[str] = []
        self.batched_calls: list[int] = []

    def get_file_info(self, paths: Any) -> Any:  # noqa: ANN401 — pyarrow's own union (str | FileSelector | list)
        if isinstance(paths, str):
            self.single_path_calls.append(paths)
        elif isinstance(paths, list):
            self.batched_calls.append(len(paths))
        return super().get_file_info(paths)


def test_the_presence_gate_probes_the_referenced_set_in_ONE_call(tmp_path: Path) -> None:
    uri = str(tmp_path / "history.lance")
    lance.write_dataset(pa.table({"id": [1, 2, 3]}), uri)
    for extra in range(4):
        lance.write_dataset(pa.table({"id": [10 + extra]}), uri, mode="append")

    fs = _CountingFs()
    scan = scan_dataset(cast(pafs.FileSystem, fs), uri, prefix=uri)
    assert scan.checked is True, scan.reason

    # The two layout DIRECTORY probes (`tree/`, `_mem_wal/`) are legitimately one call each — they ask
    # about two fixed paths, not about a set that grows with the dataset's history.
    per_path = [p for p in fs.single_path_calls if not p.endswith(("/tree", "/_mem_wal"))]
    assert per_path == [], f"{len(per_path)} sequential per-path probes for the referenced set, e.g. {per_path[:3]}"
    assert fs.batched_calls, "the referenced set must be probed as one batch"
