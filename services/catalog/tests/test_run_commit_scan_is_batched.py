"""The replay-marker scan must overlap its per-version transaction reads (CAT-CORE-10, second loop).

The finding named TWO serial loops; `_verify_fragment_data_files` was batched
(`test_fragment_verify_is_batched.py`) while `_find_run_commit` kept opening `read_transaction`
PER VERSION, serially — one object-store round trip each, on the commit hot path. pylance has no
multi-version transaction read, so the batching here is a thread pool overlapping the round trips;
the DECISIONS (skip absent, raise on unreadable, return the marker match) still run in version
order, so the answer is identical to the serial walk's.

The overlap is proven with a barrier: each `read_transaction` waits for a second concurrent caller.
A serial walk never produces one, so its first read times out — the RED this file was born with.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from lance_namespace import ServiceUnavailableError

from catalog.services import dataplane


class _Transaction:
    def __init__(self, props: dict[str, str]) -> None:
        self.transaction_properties = props


class _OverlapRequiringDataset:
    """A dataset whose transaction reads succeed ONLY when at least two run concurrently."""

    def __init__(self, versions: list[int], props_by_version: dict[int, dict[str, str]] | None = None) -> None:
        self._versions = versions
        self._props = props_by_version or {}
        # Two parties per wave; cyclic, so four reads pass as two overlapping pairs.
        self._barrier = threading.Barrier(2)

    def versions(self) -> list[dict[str, int]]:
        return [{"version": v} for v in self._versions]

    def read_transaction(self, version: int) -> _Transaction:
        try:
            self._barrier.wait(timeout=2.0)
        except threading.BrokenBarrierError as exc:
            raise TimeoutError(f"read_transaction({version}) never overlapped with a second read — the version walk is serial") from exc
        return _Transaction(self._props.get(version, {}))


def test_transaction_reads_overlap_instead_of_running_serially(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _OverlapRequiringDataset([1, 2, 3, 4])
    monkeypatch.setattr(dataplane.lance, "dataset", lambda *_a, **_kw: fake)

    # No marker anywhere -> None; a serial walk instead times out at the barrier and raises 503.
    assert dataplane._find_run_commit("s3://b/t", {}, "run-1", 0) is None


def test_the_batched_scan_still_finds_the_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Batching must not cost the answer: the marker match returns (version, rows) exactly as before."""
    marker_props = {"__lance_commit_message": "rask.ingest.run_id=run-1"}
    fake = _OverlapRequiringDataset([1, 2, 3, 4], props_by_version={3: marker_props})

    class _CountingDataset:
        def count_rows(self) -> int:
            return 7

    calls: list[tuple[Any, ...]] = []

    def _dataset(*a: Any, **kw: Any) -> Any:
        if kw.get("version") is not None or (len(a) > 1):
            calls.append((a, kw))
            return _CountingDataset()
        return fake

    monkeypatch.setattr(dataplane.lance, "dataset", _dataset)

    assert dataplane._find_run_commit("s3://b/t", {}, "run-1", 0) == (3, 7)


def test_an_unreadable_version_still_fails_closed_under_batching(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fail-closed contract survives the batching: a non-absent read error refuses, never skips."""

    class _BrokenDataset(_OverlapRequiringDataset):
        def read_transaction(self, version: int) -> _Transaction:
            raise OSError("connection reset by peer fetching the transaction file")

    broken = _BrokenDataset([1, 2])
    monkeypatch.setattr(dataplane.lance, "dataset", lambda *_a, **_kw: broken)

    with pytest.raises(ServiceUnavailableError, match="run-1"):
        dataplane._find_run_commit("s3://b/t", {}, "run-1", 0)
