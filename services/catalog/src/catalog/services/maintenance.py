"""#75 on-demand garbage collection — the operator's per-table analog of the compaction sweep's GC.

``preview_gc`` is a DRY RUN: which old versions ``cleanup_old_versions`` would reclaim, honouring the
current version, the pins on THIS ref (a version tagged on this branch, or one a child branch was cut
from, is NEVER collected), the retain-last-N window, and the age cutoff — it never mutates. ``run_gc``
performs the reclaim with the SAME tag exemption the sweep uses (``error_if_tagged_old_versions=False``),
so a long-lived promotion tag can't stall GC. Pure over a Lance dataset handle, so both are unit-testable
with a fake ``ds``.

The destructive verbs are gated by :func:`require_compactable` and :func:`require_reclaimable`, which
ask the SWEEP's gates per verb rather than one stricter gate of their own — see either for why a button
that refuses what the cron performs unattended protects nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, TypedDict

from lance_namespace import UnsupportedOperationError

from service_kit.lakehouse.base_refs import BaseRefs
from service_kit.lakehouse.features import (
    FLAG_BASE_PATHS,
    FragmentCarrier,
    ManifestCarrier,
    describe_compaction_unsupported_flags,
    describe_gc_unsupported_flags,
    gather_compaction_bases,
    manifest_feature_flags,
)
from service_kit.lakehouse.objectfs import dataset_root_probe
from service_kit.lakehouse.work_items import VECTOR_INDEX, IndexWorkItem


if TYPE_CHECKING:
    from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)

#: The read bound EVERY compaction this pod runs carries, named once because the pod has more than one
#: button that reaches `compact_files` and an unbounded one is indistinguishable from a bounded one
#: until it OOMs. #93's floor: rows are not a unit of memory, and the default batch size on a blob tier
#: read ~15 GB/thread — the OOM measured on the maintenance pod is just as available to the catalog pod
#: through any of these doors. `num_threads` is pinned for the sibling reason: Lance defaults it to the
#: HOST's parallelism, which a container's limit does not bound.
#:
#: THE ROW BOUNDS ALONE DO NOT BOUND THIS POD, and the erasure door is where that shows. 64 rows is a
#: ceiling in the unit nobody can size in advance — on a blob tier one row is the blob — so the proxy
#: is weakest exactly on the tables erasure runs over. `max_source_bytes` is the ceiling in the unit
#: that matters, and it is sized for THIS pod rather than copied from the sweep's: measured 2026-09-22
#: the catalog runs at 248Mi of a 512Mi limit, and a pass bounded at B peaks near 1.7xB resident
#: ([[LH-185]], 256 MiB -> +434 MiB). 64 MiB therefore peaks ~109 MiB against ~264 MiB of headroom.
#: A slower one-shot compaction is recoverable; an OOMKilled catalog is an outage for every caller.
COMPACTION_BOUND: Final[dict[str, int]] = {"batch_size": 64, "num_threads": 2, "max_source_bytes": 64 * 1024 * 1024}


# --------------------------------------------------------------------------- #
# The dataset surface these doors need, stated (CAT-CORE-15). Structural, not nominal
# `lance.LanceDataset`: the module docstring promises these are "pure over a Lance dataset handle, so
# both are unit-testable with a fake ``ds``", and a nominal type would make that promise unkeepable.
# Split per verb, mirroring `service_kit.lakehouse.features`' ManifestCarrier/FragmentCarrier: a door
# that only previews must not claim it can reclaim.
#
# `uri` is deliberately ABSENT from all of them and still read through `getattr(ds, "uri", "")`. A
# handle that cannot say where it lives is a case `_refuse_a_referring_datasets_source` REFUSES on
# purpose; declaring the attribute would type that branch out of existence.
# --------------------------------------------------------------------------- #


class TagIndex(Protocol):
    """``ds.tags`` — the pinned-version index. Values are pylance ``Tag`` TypedDicts at runtime.

    ROOT-SCOPED: it lists every branch's tags from any handle, each naming its own ``branch``.
    """

    def list(self) -> Mapping[str, Mapping[str, object]]: ...


class BranchIndex(Protocol):
    """``ds.branches`` — every branch and its fork point. Values are pylance ``Branch`` TypedDicts at runtime."""

    def list(self) -> Mapping[str, Mapping[str, object]]: ...


class VersionedDataset(Protocol):
    """What the GC PREVIEW reads: the current version, the version list, and the two pins — tags and
    child branches — that exempt a version from cleanup. Read-only."""

    @property
    def version(self) -> int: ...

    @property
    def tags(self) -> TagIndex: ...

    @property
    def branches(self) -> BranchIndex: ...

    def versions(self) -> Sequence[Mapping[str, Any]]: ...


class ReclaimableDataset(VersionedDataset, ManifestCarrier, Protocol):
    """The preview surface PLUS the destructive reclaim — and the manifest the gate reads first."""

    def cleanup_old_versions(self, *, older_than: timedelta, retain_versions: int | None, error_if_tagged_old_versions: bool) -> Any: ...  # noqa: ANN401 — pylance returns an untyped stats object


class Optimizer(Protocol):
    """``ds.optimize`` — the compaction/index maintenance handle."""

    def compact_files(self, **kwargs: Any) -> Any: ...  # noqa: ANN401 — pylance returns an untyped metrics object

    def optimize_indices(self) -> None: ...


class CompactableDataset(FragmentCarrier, Protocol):
    """What COMPACTION needs: the optimizer, plus the manifest + fragment walk its gate weighs."""

    @property
    def optimize(self) -> Optimizer: ...


def require_compactable(ds: CompactableDataset, storage_options: StorageOptions, protected: BaseRefs | None = None) -> None:
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


def require_reclaimable(ds: ManifestCarrier, protected: BaseRefs | None = None) -> None:
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


def _refuse_a_referring_datasets_source(ds: object, protected: BaseRefs | None) -> None:
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


def _as_utc(ts: object) -> datetime:
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


#: The default ref's name. Lance RECORDS main as null — a tag's ``branch`` and a branch's
#: ``parentBranch`` (``lance_docs/file_format.md`` "Tag File Format" / "Branch Metadata File Format") —
#: and it stores a reference spelled ``("main", n)`` as null too (measured on pylance 12.0.0), so a
#: request naming this is compared as null.
MAIN_BRANCH: Final = "main"


def _recorded_branch(branch: object) -> str | None:
    """A ref as Lance records it: ``None`` for main, the branch name otherwise."""
    if branch is None or branch == MAIN_BRANCH:
        return None
    if not isinstance(branch, str):
        raise TypeError(f"a branch is named by a str or None, got {type(branch).__name__}")
    return branch


def _tag_versions(ds: VersionedDataset, branch: str | None) -> dict[str, int]:
    """``{tag: version}`` for the tags pinning a version OF ``branch`` — the only tags its cleanup honours.

    A tag names a version within ITS branch's history, and branch histories are numbered independently
    (``lance_docs/guide.md`` Branches: "version numbers may overlap across branches"). Measured on
    pylance 12.0.0: a tag on ``work`` v3 leaves main's v3 to ``cleanup_old_versions``, and a main tag on
    v4 leaves the branch's v4, so honouring every listed tag here withholds versions the run deletes.
    """
    ref = _recorded_branch(branch)
    return {
        name: int(version)
        for name, tag in ds.tags.list().items()
        if _recorded_branch(tag.get("branch")) == ref and isinstance(version := tag.get("version"), int)
    }


def _fork_versions(ds: VersionedDataset, branch: str | None) -> dict[str, int]:
    """``{child branch: version}`` for the branches cut from ``branch`` — each pins the version it forked at.

    A branch resolves the files it inherits through its parent's history at ``parentVersion``, and
    cleanup keeps that version ("Lance ensures that cleanup does not delete files still referenced by
    any branch", ``lance_docs/guide.md``). Measured on pylance 12.0.0 at ``older_than=0``: main keeps the
    v2 a branch was cut from and deletes its untagged neighbours, and a branch keeps the version a branch
    of its own was cut from. Only DIRECT children are read: a grandchild stands on the same parent
    version its ancestor does, and Lance refuses to delete a branch another branch is cut from.
    """
    ref = _recorded_branch(branch)
    return {
        name: int(version)
        for name, meta in ds.branches.list().items()
        if _recorded_branch(meta.get("parent_branch")) == ref and isinstance(version := meta.get("parent_version"), int)
    }


class GcPreviewData(TypedDict):
    """What `preview_gc` returns — the SERVICE's shape, declared where the service is.

    A `TypedDict` and not the wire model, deliberately: no service module in this package imports
    `catalog.schemas`, because services return plain data and the endpoints own the wire shape. Naming
    the shape here keeps that layering and still lets `ty` see it.

    THE SPLAT WAS NEVER UNSAFE, which is why this is a type fix and not a runtime one. Pydantic
    validates types and required fields on `__init__`, so `GcPreview(**result)` already refuses a
    drifted key — and refuses an EXTRA one, which `model_validate` would silently ignore. What was
    missing is the failure arriving at the type checker rather than at the door. Verified: renaming one
    key here produces `ty` errors where it previously produced none.
    """

    current_version: int
    total_versions: int
    eligible_versions: list[int]
    protected_tags: dict[str, int]
    retention_days: int | None
    retain_versions: int | None


class GcRunData(TypedDict):
    """What `run_gc` returns — see :class:`GcPreviewData` for why these are TypedDicts."""

    ok: bool
    old_versions_removed: int
    bytes_removed: int


class CompactData(TypedDict):
    """What `compact_now` returns — see :class:`GcPreviewData` for why these are TypedDicts."""

    ok: bool
    fragments_removed: int
    fragments_added: int


def preview_gc(ds: VersionedDataset, *, branch: str | None, retention_days: int | None, retain_versions: int | None) -> GcPreviewData:
    """Dry-run the old-version cleanup — the versions GC would reclaim, and the tags protecting others.

    ``branch`` names the ref ``ds`` is checked out on, and it is REQUIRED because the handle cannot say:
    pylance exposes no current-branch accessor, and ``tags.list()`` / ``branches.list()`` answer the
    same root-scoped maps from every ref. Which of their pins protect a version depends on it.
    """
    current = int(ds.version)
    tags = _tag_versions(ds, branch)
    pinned = set(tags.values()) | set(_fork_versions(ds, branch).values())
    versions = sorted(ds.versions(), key=lambda v: int(v["version"]), reverse=True)
    keep_recent = {int(v["version"]) for v in versions[:retain_versions]} if retain_versions else set()
    cutoff = datetime.now(UTC) - timedelta(days=retention_days) if retention_days else None
    eligible: list[int] = []
    for v in versions:
        ver = int(v["version"])
        if ver == current or ver in pinned or ver in keep_recent:
            continue  # never the current version, one a tag or a child branch pins, or inside the retain window
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


def run_gc(ds: ReclaimableDataset, *, retention_days: int | None, retain_versions: int | None, protected: BaseRefs | None = None) -> GcRunData:
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


def compact_now(
    ds: CompactableDataset, *, target_rows_per_fragment: int | None, storage_options: StorageOptions, protected: BaseRefs | None = None
) -> CompactData:
    """#76 on-demand compaction — merge small fragments now (the operator's manual 'compact now', the analog
    of the sweep's per-table pass). Plain (non-deferred) compaction: a single on-demand pass isn't racing a
    concurrent index build, so it needs no defer_index_remap. Then keep the indices covering the new
    fragments (best-effort — a no-index dataset must not fail the compaction). Non-destructive: it writes a
    new version, never removes one.

    ``storage_options`` is what the gate's base probe is bound to, not plumbing this function itself
    uses — see :func:`require_compactable` for why it cannot be defaulted."""
    require_compactable(ds, storage_options, protected)
    size_kw: dict[str, Any] = {"target_rows_per_fragment": target_rows_per_fragment} if target_rows_per_fragment else {}
    size_kw.update(COMPACTION_BOUND)
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


class IndexableDataset(Protocol):
    """What a REBUILD needs: the two pylance create doors, plus the version they commit at.

    Structural like its siblings above, and for the same reason — this module is pure over a dataset
    handle so a fake `ds` can drive it. `version` is declared here and NOT re-read from the store:
    measured on pylance 11.0.0 (2026-09-15), `create_index(..., replace=True)` advances the open
    handle's own `version` to the committed one, matching a re-open.
    """

    @property
    def version(self) -> int: ...

    def create_index(self, column: str, *, index_type: str, **kwargs: Any) -> Any: ...  # noqa: ANN401 — pylance returns an untyped handle

    def create_scalar_index(self, column: str, *, index_type: str, **kwargs: Any) -> Any: ...  # noqa: ANN401 — as above


def rebuild_index_now(ds: IndexableDataset, item: IndexWorkItem) -> int:
    """[[LH-105]] rebuild one index in this pod, and report the version it landed at.

    The in-pod half of ``maintenance/reindex``, reached only where no index lane is configured — the
    same queue-or-inline rule ``compact_now`` serves for compaction, and for the same reason: with no
    worker subscribed, publishing a unit would accept work nothing will ever perform.

    ``replace`` is forwarded FROM THE UNIT rather than decided here, so this path and the worker build
    the same index from the same description; pylance's own defaults differ by kind, so a value
    assumed at either end would make the two lanes disagree. The unit's ``params`` are pylance's own
    keywords, read off the index being repaired — this function has no opinion on them, the rule the
    work item already states.
    """
    kwargs: dict[str, Any] = dict(item.params)
    if item.name:
        kwargs["name"] = item.name
    if item.replace is not None:
        kwargs["replace"] = item.replace
    if item.kind == VECTOR_INDEX:
        ds.create_index(item.column, index_type=item.index_type, **kwargs)
    else:
        ds.create_scalar_index(item.column, index_type=item.index_type, **kwargs)
    log.info("reindex_rebuilt_in_pod", extra={"table_id": item.table_id, "index": item.name, "column": item.column, "kind": item.kind})
    return int(ds.version)
