"""The compaction + GC core — infra-light so the discovery + aggregation logic is unit-testable.

``discover_dataset_uris`` is pure list-logic over a pyarrow filesystem; ``compact_one`` wraps the two
blocking Lance maintenance calls. Both keep IO at the edges so the orchestration can be tested with fakes.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import lance
import pyarrow.fs as pafs
from pydantic import BaseModel


log = logging.getLogger(__name__)


class DatasetResult(BaseModel):
    """What one dataset's maintenance pass did (or why it was skipped)."""

    uri: str
    #: Why the policy layer skipped this dataset this tick (``policy_disabled`` / ``policy_interval``);
    #: ``None`` when maintenance ran.
    skipped: str | None = None
    fragments_removed: int = 0
    fragments_added: int = 0
    indices_optimized: int = 0
    old_versions_removed: int = 0
    bytes_removed: int = 0
    error: str | None = None
    # Stable identifier for span aggregation (otel attributes.md: set `error.type` whenever the span
    # status is ERROR) — the exception CLASS name, never the message.
    error_type: str | None = None


def discover_dataset_uris(fs: pafs.FileSystem, bucket: str, *, max_depth: int = 3) -> list[str]:
    """Lance datasets under ``bucket`` — a directory IS a dataset iff it has a ``_versions/`` child
    (the Lance table-layout marker); any other directory is a namespace prefix and is recursed into
    (bounded by ``max_depth``). Skips ``__`` bookkeeping dirs (the catalog's ``__manifest``) and the
    control-plane registries (``_warehouses``, ``_policies``) — no dataset ever lives under them, and
    probing them is wasted S3 round-trips on the hot discovery path.

    The catalog lays top-level tables out as ``<uuid>_<table_id>/``, but the medallion cascade nests
    its datasets one level down (``medallion/raw`` …) — without the marker probe the sweep both
    reported the ``medallion/`` prefix as a failed dataset AND never maintained the real ones under it.
    """
    uris: list[str] = []

    def _walk(prefix: str, depth: int) -> None:
        for info in fs.get_file_info(pafs.FileSelector(prefix, recursive=False)):
            if info.type != pafs.FileType.Directory:
                continue
            name = info.path.rstrip("/").split("/")[-1]
            if name.startswith("__") or name in ("_warehouses", "_policies"):
                continue
            marker = fs.get_file_info(f"{info.path}/_versions")
            if marker.type == pafs.FileType.Directory:
                uris.append(f"s3://{info.path}")
            elif depth < max_depth:
                _walk(info.path, depth + 1)

    _walk(bucket, 1)
    return uris


def compact_one(
    uri: str,
    storage_options: dict[str, str],
    older_than: timedelta | None,
    retain_versions: int | None = None,
    target_rows_per_fragment: int | None = None,
) -> DatasetResult:
    """Compact small fragments + GC old versions for one dataset. Never raises — a per-dataset failure is
    captured in ``error`` so one bad dataset can't abort the whole maintenance pass."""
    try:
        ds = lance.dataset(uri, storage_options=storage_options)
    except Exception as exc:
        return DatasetResult(uri=uri, error=f"open: {exc}", error_type=type(exc).__name__)
    result = DatasetResult(uri=uri)
    try:
        # defer_index_remap: with the Fragment Reuse Index the row-id remap is deferred, so compaction and
        # index maintenance "no longer conflict" (lance_docs/guide.md:3150) — cuts the CommitConflict class
        # of maintain: failures at the source. The optimize_indices() right below folds the compacted
        # fragments into the indices; the interplay is pinned by
        # tests/unit/test_compaction_optimize.py::test_compact_one_defer_index_remap_keeps_indices_working.
        # #76 target-size tuning: the #50 policy's target_rows_per_fragment (None → Lance default sizing).
        size_kw = {"target_rows_per_fragment": target_rows_per_fragment} if target_rows_per_fragment else {}
        try:
            metrics: Any = ds.optimize.compact_files(defer_index_remap=True, **size_kw)
        except Exception as exc:
            # defer_index_remap needs row_addrs (a stable-row-id, fragment-reuse-able layout). A dataset
            # WITHOUT them — e.g. a small model-REGISTRY dataset (models$<model>) — raises
            # "defer_index_remap requires row_addrs but none were provided". Fall back to the plain
            # (non-deferred) compaction so one such dataset doesn't get reported as a sweep failure. These
            # registry datasets aren't concurrently indexed, so the CommitConflict that defer_index_remap
            # avoids isn't a risk here. Any OTHER error propagates to the outer per-dataset error capture.
            if "row_addrs" not in str(exc):
                raise
            log.warning("compact_defer_index_remap_unsupported", extra={"uri": uri, "error": str(exc)})
            metrics = ds.optimize.compact_files(**size_kw)
        result.fragments_removed = int(getattr(metrics, "fragments_removed", 0))
        result.fragments_added = int(getattr(metrics, "fragments_added", 0))
        # Keep secondary indices (vector ANN / scalar / FTS) covering the new fragments. WITHOUT this a
        # freshly-written row isn't in the index → vector/filter queries either miss it or fall back to a
        # flat scan. Index optimize is a maintenance op exactly like compaction (Lance does it distributed
        # via lance-ray; here single-process). Idempotent. Own guard so a no-index dataset can't fail it.
        try:
            ds.optimize.optimize_indices()
            # Count USER indices only: defer_index_remap creates the ``__lance_frag_reuse`` SYSTEM index,
            # which would otherwise report every ever-compacted dataset as "index maintained" forever —
            # phantom signal in the reclaim metrics (review 2026-07-10, verified on pylance 8.0.0).
            result.indices_optimized = len([ix for ix in ds.list_indices() if not ix["name"].startswith("__")])
        except Exception as exc:
            log.warning("optimize_indices_skipped", extra={"uri": uri, "error": str(exc)})
        # error_if_tagged_old_versions=False: tagged versions are EXEMPT from GC (they survive until the tag
        # is deleted). The default (True) RAISES once any tag ages past older_than — which, since the catalog
        # creates long-lived promotion tags, would permanently stall GC for that dataset (the raise is caught
        # and recorded as error, reclaiming nothing). We want GC to skip tagged versions and reclaim the rest.
        # retain_versions (#50 policy override): with ``older_than=None`` it is pure count-based
        # retention ("keep exactly the last N"); when both are set, a version must clear *both* bounds
        # to be removed. Never pass both as None — pylance then substitutes a 14-day default.
        stats: Any = ds.cleanup_old_versions(older_than=older_than, retain_versions=retain_versions, error_if_tagged_old_versions=False)
        result.old_versions_removed = int(getattr(stats, "old_versions", 0))
        result.bytes_removed = int(getattr(stats, "bytes_removed", 0))
    except Exception as exc:
        result.error = f"maintain: {exc}"
        result.error_type = type(exc).__name__
    return result
