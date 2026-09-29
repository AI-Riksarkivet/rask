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

import pytest

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

    def version_refs(self) -> list[dict[str, int]]:
        """`version_refs`, matching the accessor the scan calls — a double carrying only `versions()`
        answers an `AttributeError` that the caller reports as an unreadable store, so the double
        would fail the test for a reason the test is not about."""
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
