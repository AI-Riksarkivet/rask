"""ANN-08 — the version history's per-version reads must be cheap and must not run in lockstep.

`GET /api/annotations/{doc}/{speech}/{chunk}/versions` answers one row per Lance version, each row
carrying the count of THIS unit's annotations at that version. There is no way to answer that
without touching each version — but there were two ways to make it worse, and both were taken:

* every version was counted by materializing an Arrow table of matching `id`s and reading
  `.num_rows`, where the count is a pushdown the format already answers;
* the versions were walked in a `for` loop, so the wall clock was `limit` × (one manifest open +
  one filtered scan) — up to 200 of them, in series, on S3, holding a threadpool worker.

The catalog-mode branch has the identical shape with an HTTP round-trip per version, which is worse.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from annotator.annotations.versions import local_annotation_versions


class _Concurrency:
    """Records the high-water mark of simultaneous in-flight counts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live = 0
        self.peak = 0

    def __enter__(self) -> None:
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
        time.sleep(0.02)

    def __exit__(self, *_exc: object) -> None:
        with self._lock:
            self._live -= 1


class _Snapshot:
    def __init__(self, count: int, gauge: _Concurrency | None) -> None:
        self._count = count
        self._gauge = gauge

    def count_rows(self, filter: str | None = None) -> int:  # noqa: A002 - pylance's own parameter name
        if self._gauge is None:
            return self._count
        with self._gauge:
            return self._count

    def to_table(self, **_kwargs: Any) -> Any:
        raise AssertionError("the history materialized an Arrow table of ids where the format answers the count directly")


class _Dataset:
    """A `lance.LanceDataset` double: `versions()` newest-LAST, `checkout_version` per version."""

    def __init__(self, total: int, gauge: _Concurrency | None = None) -> None:
        self._total = total
        self._gauge = gauge
        self.checked_out: list[int] = []

    def versions(self) -> list[dict[str, Any]]:
        return [{"version": n, "timestamp": f"2026-08-30T00:00:{n:02d}"} for n in range(1, self._total + 1)]

    def checkout_version(self, version: int) -> _Snapshot:
        self.checked_out.append(version)
        return _Snapshot(count=version, gauge=self._gauge)


@pytest.mark.parametrize("limit", [1])
def test_the_limit_still_caps_the_snapshots_that_are_opened(limit: int) -> None:
    dataset = _Dataset(total=10)

    rows = local_annotation_versions(dataset, "doc_id = 'a'", limit=limit)

    assert len(rows) == limit
    assert len(dataset.checked_out) == limit, "a capped listing must not open the versions it will not return"
