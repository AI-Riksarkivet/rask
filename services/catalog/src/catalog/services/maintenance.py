"""#75 on-demand garbage collection — the operator's per-table analog of the compaction sweep's GC.

``preview_gc`` is a DRY RUN: which old versions ``cleanup_old_versions`` would reclaim, honouring the
current version, tag pins (a tagged version is NEVER collected), the retain-last-N window, and the age
cutoff — it never mutates. ``run_gc`` performs the reclaim with the SAME tag exemption the sweep uses
(``error_if_tagged_old_versions=False``), so a long-lived promotion tag can't stall GC. Pure over a Lance
dataset handle, so both are unit-testable with a fake ``ds``.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from lance_namespace import UnsupportedOperationError

from service_kit.lakehouse.features import unsupported_features


def require_maintainable(ds: Any) -> None:
    """#121: refuse a dataset whose manifest feature flags this pass cannot correctly rewrite.

    The SWEEP got this gate at #64 — measured then: compacting a shallow clone (flag 16) returned
    ``fragments_removed=8, error=None`` and silently materialised a full local copy of data the clone
    had only referenced. These on-demand doors are the SAME operations wired to a button in the table
    UI, and they shipped with no gate at all — the "actively destructive" defect stayed one click
    away after the sweep was fixed. Checked BEFORE any rewrite; the refusal names the flags, because
    an operator needs to know they are looking at a shallow clone, not a broken dataset.
    """
    reason = unsupported_features(ds)
    if reason is not None:
        raise UnsupportedOperationError(
            f"maintenance refused: {reason}. This dataset's layout carries features the rewrite would corrupt (the sweep refuses it for the same reason)."
        )


def _as_utc(ts: Any) -> datetime:
    """Coerce a version timestamp to an aware UTC datetime; an unknown shape is treated as 'now' so it is
    never eligible for collection (fail-safe — GC must not remove a version whose age it can't read).

    A NAIVE datetime from ``ds.versions()`` is host-LOCAL wall-clock (pylance builds it with
    ``datetime.fromtimestamp(ns/1e9)`` — no tzinfo), so it must be ``astimezone(UTC)`` (interpret-as-local,
    convert), NOT ``replace(tzinfo=UTC)`` (relabel local as UTC). On a non-UTC host the relabel skewed the
    dry-run's age by the host offset, so ``preview_gc`` reported versions as protected that ``run_gc`` — which
    compares the manifest's true-UTC instant — then reclaimed, breaking the pre-flight. (audit 2026-07-20)
    """
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.astimezone(UTC)
    return datetime.now(UTC)


def _tag_versions(ds: Any) -> dict[str, int]:
    """``{tag: version}`` — the pinned versions, exempt from GC (pylance's Tag is a TypedDict at runtime)."""
    out: dict[str, int] = {}
    for name, tag in ds.tags.list().items():
        entry = tag if isinstance(tag, dict) else {"version": getattr(tag, "version", None)}
        version = entry.get("version")
        if version is not None:
            out[name] = int(version)
    return out


def preview_gc(ds: Any, *, retention_days: int | None, retain_versions: int | None) -> dict[str, Any]:
    """Dry-run the old-version cleanup — the versions GC would reclaim, and the tags protecting others."""
    current = int(ds.version)
    tags = _tag_versions(ds)
    tagged = set(tags.values())
    versions = sorted(ds.versions(), key=lambda v: int(v["version"]), reverse=True)
    keep_recent = {int(v["version"]) for v in versions[:retain_versions]} if retain_versions else set()
    cutoff = datetime.now(UTC) - timedelta(days=retention_days) if retention_days else None
    eligible: list[int] = []
    for v in versions:
        ver = int(v["version"])
        if ver == current or ver in tagged or ver in keep_recent:
            continue  # never the current version, a tag-pinned one, or inside the retain window
        ts = v.get("timestamp")
        if cutoff is not None and ts is not None and _as_utc(ts) > cutoff:
            continue  # too new to reclaim under the age cutoff
        eligible.append(ver)
    return {
        "current_version": current,
        "total_versions": len(versions),
        "eligible_versions": eligible,
        "protected_tags": tags,
        "retention_days": retention_days,
        "retain_versions": retain_versions,
    }


def run_gc(ds: Any, *, retention_days: int | None, retain_versions: int | None) -> dict[str, Any]:
    """Reclaim old versions (DESTRUCTIVE). Tagged versions are exempt, exactly like the compaction sweep."""
    require_maintainable(ds)
    older_than = timedelta(days=retention_days) if retention_days else timedelta(0)
    stats: Any = ds.cleanup_old_versions(older_than=older_than, retain_versions=retain_versions, error_if_tagged_old_versions=False)
    return {
        "ok": True,
        "old_versions_removed": int(getattr(stats, "old_versions", 0) or 0),
        "bytes_removed": int(getattr(stats, "bytes_removed", 0) or 0),
    }


def compact_now(ds: Any, *, target_rows_per_fragment: int | None) -> dict[str, Any]:
    """#76 on-demand compaction — merge small fragments now (the operator's manual 'compact now', the analog
    of the sweep's per-table pass). Plain (non-deferred) compaction: a single on-demand pass isn't racing a
    concurrent index build, so it needs no defer_index_remap. Then keep the indices covering the new
    fragments (best-effort — a no-index dataset must not fail the compaction). Non-destructive: it writes a
    new version, never removes one."""
    require_maintainable(ds)
    size_kw: dict[str, Any] = {"target_rows_per_fragment": target_rows_per_fragment} if target_rows_per_fragment else {}
    # #93's floor, applied to this door too: rows are not a unit of memory, and the default batch
    # size on a blob tier read ~15 GB/thread — the OOM measured on the maintenance pod is just as
    # available to the catalog pod through this button.
    size_kw["batch_size"] = 64
    size_kw["num_threads"] = 2
    metrics: Any = ds.optimize.compact_files(**size_kw)
    # a no-index dataset / unindexed column must not fail the compaction
    with contextlib.suppress(Exception):
        ds.optimize.optimize_indices()
    return {
        "ok": True,
        "fragments_removed": int(getattr(metrics, "fragments_removed", 0) or 0),
        "fragments_added": int(getattr(metrics, "fragments_added", 0) or 0),
    }
