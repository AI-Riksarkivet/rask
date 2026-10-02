"""The run-commit scan must overlap its per-version transaction reads (CAT-CORE-10, second loop).

The finding named TWO serial loops; `_verify_fragment_data_files` was batched
(`test_fragment_verify_is_batched.py`) while `_find_run_commit` kept opening `read_transaction`
PER VERSION, serially — one object-store round trip each, on the commit hot path. pylance has no
multi-version transaction read, so the batching here is a thread pool overlapping the round trips;
the DECISIONS (skip absent, raise on unreadable, return the version holding the run's fragments) still
run in version order, so the answer is identical to the serial walk's.

The overlap is proven with a barrier: each `read_transaction` waits for a second concurrent caller.
A serial walk never produces one, so its first read times out — the RED this file was born with.
"""

from __future__ import annotations

import json
import threading

import lance
import pytest

from catalog.services import dataplane


_RUNS_FRAGMENT = {
    "id": 0,
    "files": [
        {"path": "run.lance", "fields": [0], "column_indices": [0], "file_major_version": 2, "file_minor_version": 2, "file_size_bytes": 1, "base_id": None}
    ],
    "physical_rows": 1,
}


class _OverlapRequiringDataset:
    """A dataset whose transaction reads succeed ONLY when at least two run concurrently."""

    def __init__(self, versions: list[int]) -> None:
        self._versions = versions
        # Two parties per wave; cyclic, so four reads pass as two overlapping pairs.
        self._barrier = threading.Barrier(2)

    def version_refs(self) -> list[dict[str, int]]:
        """`version_refs`, matching the accessor the scan calls — a double carrying only `versions()`
        answers an `AttributeError` that the caller reports as an unreadable store, so the double
        would fail the test for a reason the test is not about."""
        return [{"version": v} for v in self._versions]

    def read_transaction(self, version: int) -> lance.Transaction:
        """A stranger's empty append, once a second read is in flight."""
        try:
            self._barrier.wait(timeout=2.0)
        except threading.BrokenBarrierError as exc:
            raise TimeoutError(f"read_transaction({version}) never overlapped with a second read — the version walk is serial") from exc
        return lance.Transaction(read_version=version - 1, operation=lance.LanceOperation.Append([]), uuid=f"stranger-{version}")


def test_transaction_reads_overlap_instead_of_running_serially(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _OverlapRequiringDataset([1, 2, 3, 4])
    monkeypatch.setattr(dataplane.lance, "dataset", lambda *_a, **_kw: fake)

    fragments = [lance.FragmentMetadata.from_json(json.dumps(_RUNS_FRAGMENT))]

    # No version holds the run's fragments -> None; a serial walk instead times out at the barrier and raises 503.
    assert dataplane._find_run_commit("s3://b/t", {}, fragments, read_version=0, run_id="run-1") is None
