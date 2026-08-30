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
from typing import Any, cast

import pytest

from annotator.annotations import versions as module
from annotator.annotations.versions import AnnotationVersion, catalog_annotation_versions, local_annotation_versions
from service_kit.exceptions import NotFoundError
from service_kit.lancekit.reader import CatalogTableReader, CatalogVersion


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


def test_the_local_history_counts_by_pushdown_not_by_materializing_ids() -> None:
    """`to_table(columns=["id"]).num_rows` builds a table only to measure it."""
    dataset = _Dataset(total=3)

    rows = local_annotation_versions(dataset, "doc_id = 'a'", limit=3)

    assert rows == [
        AnnotationVersion(version=3, timestamp="2026-08-30T00:00:03", count=3),
        AnnotationVersion(version=2, timestamp="2026-08-30T00:00:02", count=2),
        AnnotationVersion(version=1, timestamp="2026-08-30T00:00:01", count=1),
    ]


def test_the_local_history_reads_its_snapshots_concurrently() -> None:
    """`limit` serial snapshot opens is `limit` × the S3 latency, in the request's own wall clock."""
    gauge = _Concurrency()
    dataset = _Dataset(total=16, gauge=gauge)

    rows = local_annotation_versions(dataset, "doc_id = 'a'", limit=16)

    assert [row.version for row in rows] == list(range(16, 0, -1)), "newest-first order must survive the fan-out"
    assert gauge.peak > 1, "the snapshots are still read one after another"
    assert gauge.peak <= module.VERSION_FANOUT, "the fan-out must be bounded, not one thread per version"


class _Reader:
    """A `CatalogTableReader` double — one HTTP round-trip per version, which is the worse half."""

    def __init__(self, total: int, gauge: _Concurrency | None = None, reclaimed: set[int] | None = None) -> None:
        self._total = total
        self._gauge = gauge
        self._reclaimed = reclaimed or set()

    def versions(self, limit: int) -> list[CatalogVersion]:
        return [CatalogVersion(version=n, timestamp_millis=1_753_300_000_000 + n) for n in range(self._total, 0, -1)][:limit]

    def count_rows(self, where: str, *, version: int) -> int:
        if version in self._reclaimed:
            raise NotFoundError(f"version {version} was reclaimed")
        if self._gauge is None:
            return version
        with self._gauge:
            return version


def test_the_catalog_history_reads_its_versions_concurrently() -> None:
    gauge = _Concurrency()

    # `cast`, not a suppression: `CatalogTableReader` is a concrete class over an HTTP transport,
    # and the two methods this function uses are the whole contract a double has to satisfy.
    rows = catalog_annotation_versions(cast(CatalogTableReader, _Reader(16, gauge)), "doc_id = 'a'", limit=16)

    assert [row.version for row in rows] == list(range(16, 0, -1))
    assert gauge.peak > 1, "the catalog counts are still issued one round-trip at a time"
    assert gauge.peak <= module.VERSION_FANOUT


def test_a_version_reclaimed_mid_listing_is_still_dropped_rather_than_failing_the_whole_read() -> None:
    """The retention race the sequential loop handled — the fan-out must keep handling it."""
    rows = catalog_annotation_versions(cast(CatalogTableReader, _Reader(4, reclaimed={3})), "doc_id = 'a'", limit=4)

    assert [row.version for row in rows] == [4, 2, 1]


def test_an_unknown_catalog_table_is_still_an_empty_history() -> None:
    class _Missing:
        def versions(self, limit: int) -> list[CatalogVersion]:
            raise NotFoundError("no such table")

    assert catalog_annotation_versions(cast(CatalogTableReader, _Missing()), "doc_id = 'a'", limit=4) == []


@pytest.mark.parametrize("limit", [1, 2])
def test_the_limit_still_caps_the_snapshots_that_are_opened(limit: int) -> None:
    dataset = _Dataset(total=10)

    rows = local_annotation_versions(dataset, "doc_id = 'a'", limit=limit)

    assert len(rows) == limit
    assert len(dataset.checked_out) == limit, "a capped listing must not open the versions it will not return"
