"""#75 on-demand garbage collection — the operator's per-table analog of the compaction sweep's GC.

``preview_gc`` is a DRY RUN: which old versions ``cleanup_old_versions`` would reclaim, honouring the
current version, tag pins (a tagged version is NEVER collected), the retain-last-N window, and the age
cutoff — it never mutates. ``run_gc`` performs the reclaim with the SAME tag exemption the sweep uses
(``error_if_tagged_old_versions=False``), so a long-lived promotion tag can't stall GC. Pure over a Lance
dataset handle, so both are unit-testable with a fake ``ds``.

The destructive verbs are gated by :func:`require_compactable` and :func:`require_reclaimable`, which
ask the SWEEP's gates per verb rather than one stricter gate of their own — see either for why a button
that refuses what the cron performs unattended protects nothing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pyarrow.fs as pafs
from lance_namespace import UnsupportedOperationError

from service_kit.lakehouse.base_refs import BaseRefs, protected_roots
from service_kit.lakehouse.features import (
    FLAG_BASE_PATHS,
    describe_compaction_unsupported_flags,
    describe_gc_unsupported_flags,
    gather_compaction_bases,
    manifest_feature_flags,
)
from service_kit.lakehouse.objectfs import dataset_root_probe, fs_and_base


if TYPE_CHECKING:
    from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)


def require_compactable(ds: Any, storage_options: StorageOptions, protected: BaseRefs | None = None) -> None:
    """#121 + #114 for the COMPACT door: refuse a dataset this button must not rewrite.

    **THE GATE IS THE SWEEP'S, per verb — not a stricter one.** It asks
    :func:`~service_kit.lakehouse.features.describe_compaction_unsupported_flags`, exactly what
    ``maintenance.services.optimize`` asks before its own ``compact_files``, and it gathers the same
    three readings to answer it (the manifest's ``BasePath.is_dataset_root``, an object-store probe of
    each base, and whether any ``DataFile`` resolves through one). It used to ask the flags-only
    :func:`~service_kit.lakehouse.features.unsupported_features`, which refuses ``base_paths`` in
    every form — and once the sweep's gate moved to evidence (#6), this door was the STRICTER of the
    two while its refusal told the operator the sweep agreed with it.

    Strictness here protects nothing, which is why the divergence was closed rather than documented:
    the cron runs these same operations unattended against these same datasets every tick, so a
    button that refuses what the cron performs does not prevent the rewrite — it only denies the
    operator the remedy. The concrete cost was total: ``ingest/lander.py::create_empty`` and
    ``medallion/services/compute.py`` register an external blob prefix through ``initial_bases``, so
    every ingest bronze table and every medallion tier sets flag 16 and every "compact now" on the
    estate's most-fragmented tables answered with a refusal that was measurably false about them (4
    fragments -> 1, the base directory byte-identical, 20/20 external payloads still resolving).

    The relaxation is not a loosening of posture: that gate FAILS CLOSED on every unknown — no
    evidence, an unparseable ``BasePath``, an unanswerable probe, an unreadable fragment list — and it
    still refuses a real shallow clone (a cost refusal: compacting one materialises the shared data
    into its own root, 1,072 -> 108,199 bytes against a 119,693-byte base). ``storage_options`` is
    REQUIRED rather than defaulted because the probe must be bound to the store this dataset lives in:
    a manifest states its base as ``/bucket/ns/t.lance`` while the dataset is ``s3://bucket/…``, and
    probing the schemeless spelling reads it as a local path, finds nothing, and answers "not a
    dataset root" — a wrong PERMIT on a real clone, the one direction this gate must never take.

    **Somebody else's layout (#114) is the other half, and no flag can see it** — see
    :func:`_refuse_a_referring_datasets_source`.
    """
    reader, writer = manifest_feature_flags(ds)
    location = str(getattr(ds, "uri", "") or "")
    bases = (
        # Gathered ONLY when the flag is set — this is the one place the gate costs IO, and almost no
        # dataset declares a base. A dataset that cannot say where it lives cannot have its bases
        # probed either, so it reaches the gate with no evidence, which the gate reads as a refusal.
        gather_compaction_bases(ds, dataset_root_probe(location, storage_options)) if location and (reader | writer) & FLAG_BASE_PATHS else None
    )
    if (reason := describe_compaction_unsupported_flags(reader, writer, bases)) is not None:
        raise UnsupportedOperationError(
            f"maintenance refused: {reason}. Compacting here could rewrite bytes this dataset does not own — "
            "the sweep's compaction gate weighs this same evidence and refuses it too."
        )
    _refuse_a_referring_datasets_source(ds, protected)


def require_reclaimable(ds: Any, protected: BaseRefs | None = None) -> None:
    """#121 + #114 for the GC door: refuse a dataset whose versions this button must not reclaim.

    **THE GATE IS THE SWEEP'S, per verb** — :func:`~service_kit.lakehouse.features.describe_gc_unsupported_flags`,
    which is what ``maintenance.services.optimize`` asks before its own ``cleanup_old_versions``.
    Version reclamation and index maintenance are ROOT-SCOPED, so ``base_paths`` (16) does not
    endanger them: measured on pylance 9.0.0 across six cleanup shapes and ten repeat cycles, one
    ``cleanup_old_versions`` on a clone with dead fragments on both sides removed the 2 clone-owned
    files, left all 4 base-owned ones, and the base still read in a fresh process. Everything else —
    flag 64, anything unknown — refuses exactly as before.

    This door used to ask the flags-only gate and therefore refused a clone the cron reclaims on a
    120 s timer; the refusal preserved nothing and claimed the sweep agreed with it.
    """
    reader, writer = manifest_feature_flags(ds)
    if (reason := describe_gc_unsupported_flags(reader, writer)) is not None:
        raise UnsupportedOperationError(
            f"maintenance refused: {reason}. Reclaiming versions here would act on a layout this pass cannot correctly rewrite — "
            "the sweep's version-reclamation gate refuses it too."
        )
    _refuse_a_referring_datasets_source(ds, protected)


def _refuse_a_referring_datasets_source(ds: Any, protected: BaseRefs | None) -> None:
    """#114: refuse a dataset ANOTHER one resolves its files through — the half no flag check can see.

    Flag 16 marks the dataset that SPANS bases — the CLONE. The dataset in danger here is the SOURCE,
    and it carries no flag and no ``base_paths`` of its own; measured, source ``(0, 0)`` with no
    base_paths against clone ``(16, 16)`` naming the source. Its data files are the only copy the
    clone resolves through, so this door's verbs destroy them: ``compact_files`` ADDS the merged file
    (4 -> 5, the clone still opens) and ``cleanup_old_versions`` then removes the obsoleted originals
    (-> 1), after which the clone will not open in a fresh process. The evidence lives only on the
    referring side, so it has to be collected across the estate first — :func:`sibling_base_refs`.

    The sweep got that guard at #114 and this door did not, which is not a smaller version of the
    same defect: it is the same irreversible deletion, one click away instead of one cron tick away.

    ``protected`` is the collected map, or ``None`` when the caller collected none. A map that IS
    supplied is checked against this dataset's ``uri``, and a dataset that cannot say where it lives
    is REFUSED rather than waved through — "we could not tell" reads as the refusal here for the same
    reason it does everywhere else in this gate: what it guards is unrecoverable.
    """
    if protected is None:
        return
    location = str(getattr(ds, "uri", "") or "")
    if not location:
        raise UnsupportedOperationError(
            "maintenance refused: the estate's base references were collected but this dataset reports no location to check them against."
        )
    if (root := protected.is_protected(location)) is not None:
        raise UnsupportedOperationError(
            f"maintenance refused: another dataset resolves its files through {root} (shallow clone / multi-base) — "
            "compacting or reclaiming here would break it (the sweep's base-reference guard refuses it for the same reason)."
        )


def sibling_base_refs(location: str, storage_options: StorageOptions) -> BaseRefs:
    """Every root referenced by a dataset laid out ALONGSIDE ``location`` — the map
    :func:`require_compactable` and :func:`require_reclaimable` check against.

    THE BOUND IS THE WAREHOUSE ROOT, and it is stated rather than implied. A referrer in some other
    warehouse is invisible here, exactly as a referrer outside its configured buckets is invisible to
    the sweep; what this rules out is the case that can actually happen, since ``shallow_clone``
    resolves through a path and the catalog's own tables share one root. The alternative — walking
    every warehouse on every button press — buys coverage of a shape nothing in this estate creates
    at a cost paid on every click.

    The listing is ONE non-recursive call because the layout is flat: the ``dir`` backend does not
    nest a table under its namespace, it encodes both into one directory name
    (``<uuid8>_<namespace>$<table>``) directly under the root. Anything that is not a Lance dataset
    simply fails to open and lands in ``unreadable``, which the caller can see.
    """
    root = location.rstrip("/").rsplit("/", 1)[0]
    fs, base = fs_and_base(root, storage_options)
    siblings = [
        f"{root.rstrip('/')}/{info.path.rstrip('/').rsplit('/', 1)[-1]}"
        for info in fs.get_file_info(pafs.FileSelector(base, recursive=False, allow_not_found=True))
        if info.type == pafs.FileType.Directory
    ]
    return protected_roots(siblings, storage_options)


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


def run_gc(ds: Any, *, retention_days: int | None, retain_versions: int | None, protected: BaseRefs | None = None) -> dict[str, Any]:
    """Reclaim old versions (DESTRUCTIVE). Tagged versions are exempt, exactly like the compaction sweep.

    THE STEP THAT ACTUALLY DELETES, which is why ``protected`` matters most here: measured, compaction
    adds the merged file and removes nothing, and it is this call that then removes the obsoleted
    originals a shallow clone still resolves through. See :func:`require_reclaimable`, which gates
    this door on the sweep's own root-scoped gate — the same one the cron applies to the same dataset
    every tick.
    """
    require_reclaimable(ds, protected)
    older_than = timedelta(days=retention_days) if retention_days else timedelta(0)
    stats: Any = ds.cleanup_old_versions(older_than=older_than, retain_versions=retain_versions, error_if_tagged_old_versions=False)
    return {
        "ok": True,
        "old_versions_removed": int(getattr(stats, "old_versions", 0) or 0),
        "bytes_removed": int(getattr(stats, "bytes_removed", 0) or 0),
    }


def compact_now(ds: Any, *, target_rows_per_fragment: int | None, storage_options: StorageOptions, protected: BaseRefs | None = None) -> dict[str, Any]:
    """#76 on-demand compaction — merge small fragments now (the operator's manual 'compact now', the analog
    of the sweep's per-table pass). Plain (non-deferred) compaction: a single on-demand pass isn't racing a
    concurrent index build, so it needs no defer_index_remap. Then keep the indices covering the new
    fragments (best-effort — a no-index dataset must not fail the compaction). Non-destructive: it writes a
    new version, never removes one.

    ``storage_options`` is what the gate's base probe is bound to, not plumbing this function itself
    uses — see :func:`require_compactable` for why it cannot be defaulted."""
    require_compactable(ds, storage_options, protected)
    size_kw: dict[str, Any] = {"target_rows_per_fragment": target_rows_per_fragment} if target_rows_per_fragment else {}
    # #93's floor, applied to this door too: rows are not a unit of memory, and the default batch
    # size on a blob tier read ~15 GB/thread — the OOM measured on the maintenance pod is just as
    # available to the catalog pod through this button.
    size_kw["batch_size"] = 64
    size_kw["num_threads"] = 2
    metrics: Any = ds.optimize.compact_files(**size_kw)
    # Index work is best-effort — a no-index dataset or an unindexed column must not cost this door the
    # compaction that already succeeded. `BaseException`, deliberately, and NOT `suppress(Exception)`:
    # pylance PANICS for real (`pyo3_runtime.PanicException: not yet implemented` out of `index_stats`,
    # an unimplemented arm for JSON indices), and a pyo3 panic derives from BaseException, so
    # `suppress(Exception)` let it straight through — the same defect the sweep's own two guards
    # (`optimize.compact_one`, `index_health.inspect_indices`) were written as `except BaseException`
    # to close, after one panicking index answered an entire sweep HTTP 500.
    #
    # LOUD, not silent. A suppressed exception says nothing at all; this door is an operator pressing a
    # button, and "the compaction worked, the index maintenance did not" is exactly what they need to
    # know. KeyboardInterrupt/SystemExit are re-raised — swallowing a shutdown turns it into a hang.
    try:
        ds.optimize.optimize_indices()
    except BaseException as exc:  # noqa: BLE001 — a Rust PANIC is not an Exception; see above
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        log.warning("compact_now_optimize_indices_skipped", extra={"error": str(exc), "error_type": type(exc).__name__})
    return {
        "ok": True,
        "fragments_removed": int(getattr(metrics, "fragments_removed", 0) or 0),
        "fragments_added": int(getattr(metrics, "fragments_added", 0) or 0),
    }
