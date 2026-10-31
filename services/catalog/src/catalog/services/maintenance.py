"""#75 on-demand garbage collection — the operator's per-table analog of the maintenance sweep's version expiry.

``preview_gc`` is a DRY RUN: which old versions ``cleanup_old_versions`` would reclaim, honouring the
current version, the pins on THIS ref (a version tagged on this branch, or one a child branch was cut
from, is NEVER collected), the retain-last-N window, and the age cutoff — it never mutates. ``run_gc``
performs the reclaim with the SAME tag exemption the sweep uses (``error_if_tagged_old_versions=False``),
so a long-lived promotion tag can't stall GC. ``delete_versions`` backs the spec's version-delete door
with the same reclaim, for named versions, and refuses a pinned one instead of skipping it. Pure over a
Lance dataset handle, so these are unit-testable with a fake ``ds``.

The reclaims are gated by :func:`require_reclaimable`, the sweep's own root-scoped gate. :func:`require_compactable`
is the layout gate a compaction a client stages is judged against at ``version/create``, the one maintenance asks before
it lists a table as due for compaction.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, TypedDict

from lance_namespace import InvalidInputError, InvalidTableStateError, UnsupportedOperationError

from service_kit.lakehouse import branch_layout
from service_kit.lakehouse.base_refs import BaseRefs
from service_kit.lakehouse.compaction_gate import compaction_refusal
from service_kit.lakehouse.features import FragmentCarrier, ManifestCarrier, describe_gc_unsupported_flags, manifest_feature_flags
from service_kit.lakehouse.version_retention import Retention, fork_versions, reclaimable_versions, recorded_branch, tag_versions


if TYPE_CHECKING:
    from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# The dataset surface these doors need, stated (CAT-CORE-15). Structural, not nominal
# `lance.LanceDataset`: the module docstring promises these are "pure over a Lance dataset handle, so
# both are unit-testable with a fake ``ds``", and a nominal type would make that promise unkeepable.
# Split per verb, mirroring `service_kit.lakehouse.features`' ManifestCarrier/FragmentCarrier: a door
# that only previews must not claim it can reclaim.
#
# `uri` is deliberately ABSENT from all of them and still read through `getattr(ds, "uri", "")`. A
# handle that cannot say where it lives is a case `refuse_a_referring_datasets_source` REFUSES on
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


class VersionDeletableDataset(VersionedDataset, ManifestCarrier, Protocol):
    """The preview surface PLUS the reclaim of NAMED versions — and the manifest the gate reads first."""

    def cleanup_old_versions(self, *, versions: list[int], error_if_tagged_old_versions: bool) -> Any: ...  # noqa: ANN401 — pylance returns an untyped stats object


def require_compactable(ds: FragmentCarrier, table_location: str, storage_options: StorageOptions) -> None:
    """Refuse compacting a dataset whose layout makes a rewrite copy bytes it does not own (#121).

    The gate maintenance asks before it lists a table as due for compaction, one function
    (:func:`~service_kit.lakehouse.compaction_gate.compaction_refusal`), so a compaction is judged by the evidence that
    listed it: :func:`~service_kit.lakehouse.features.describe_compaction_unsupported_flags` over three readings
    (the manifest's ``BasePath.is_dataset_root``, an object-store probe of each base, and whether any ``DataFile``
    resolves through one). It fails closed on every unknown and refuses a real shallow clone, whose compaction
    materialises the shared data into its own root (measured: 1,072 -> 108,199 bytes against a 119,693-byte base).
    ``table_location`` is the root of the table ``ds`` belongs to, for a branch's handle too: a branch's bases inside
    the table are its own history. ``storage_options`` binds the probe to the table's store: a base stated as
    ``/bucket/ns/t.lance`` and probed as a local path finds nothing, which would permit a real clone.

    Raises:
        UnsupportedOperationError: The layout is one a compaction must not rewrite, or the evidence is unreadable.
    """
    if (reason := compaction_refusal(ds, table_location, storage_options)) is not None:
        raise UnsupportedOperationError(f"compaction refused: {reason}. Compacting here could rewrite bytes this dataset does not own.")


def require_reclaimable(ds: ManifestCarrier, protected: BaseRefs | None = None) -> None:
    """#121 + #114 for the GC door: refuse a dataset whose versions this button must not reclaim.

    **THE GATE IS THE SWEEP'S, per verb** — :func:`~service_kit.lakehouse.features.describe_gc_unsupported_flags`,
    which is what ``maintenance.services.expiry`` asks before its own ``cleanup_old_versions``.
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
    refuse_a_referring_datasets_source(ds, protected)


class ReferredSourceError(UnsupportedOperationError):
    """A rewrite or reclaim refused because other datasets resolve their files through this one (#114).

    Carries their locations, which the refusal itself does not name: a referrer may be a table the caller holds no grant on
    ([[LH-436]]), so each door names them to whoever may see them (``fga_deps.referring_datasets``, [[LH-414]]).
    """

    def __init__(self, message: str, *, referrers: Sequence[str]) -> None:
        super().__init__(message)
        self.referrers = tuple(referrers)


def refuse_a_referring_datasets_source(ds: object, protected: BaseRefs | None) -> None:
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
        raise ReferredSourceError(
            f"maintenance refused: another dataset resolves its files through {root} (shallow clone / multi-base) — "
            "compacting or reclaiming here would break it (the sweep's base-reference guard refuses it for the same reason).",
            referrers=sorted(protected.referrers.get(root, ())),
        )


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


def preview_gc(ds: VersionedDataset, *, branch: str | None, retention_days: int | None, retain_versions: int | None) -> GcPreviewData:
    """Dry-run the old-version cleanup — the versions GC would reclaim, and the tags protecting others.

    ``branch`` names the ref ``ds`` is checked out on, and it is REQUIRED because the handle cannot say:
    pylance exposes no current-branch accessor, and ``tags.list()`` / ``branches.list()`` answer the
    same root-scoped maps from every ref. Which of their pins protect a version depends on it.
    """
    current = int(ds.version)
    retention = Retention(older_than=timedelta(days=retention_days) if retention_days else None, retain_versions=retain_versions)
    eligible = reclaimable_versions(ds, branch=branch, retention=retention, now=datetime.now(UTC))
    return {
        "current_version": current,
        "total_versions": len(ds.versions()),
        "eligible_versions": eligible,
        "protected_tags": tag_versions(ds, branch),
        "retention_days": retention_days,
        "retain_versions": retain_versions,
    }


def refuse_a_reclaim_over_nested_branches(ds: VersionedDataset, branch: str | None) -> None:
    """Refuse reclaiming a branch that has another branch inside its directory (409, [[LH-203]]).

    A branch handle lists what lies under ``tree/<branch>/_versions/`` as its own history, so branch
    ``a/_versions``'s manifests read as ``a``'s versions and a reclaim of ``a`` deletes them: measured on
    pylance 12.0.0, a cold read of ``a/_versions`` then fails ``Not found``. The create door refuses such
    names, so only a table that predates that refusal carries one. Main has no such directory.
    """
    ref = recorded_branch(branch)
    if ref is not None and (nested := branch_layout.branches_inside(ref, ds.branches.list())):
        raise InvalidTableStateError(
            f"refused: branches {nested} lie inside branch {ref!r}'s directory, and a reclaim of {ref!r} would delete their files. Nothing was deleted"
        )


def run_gc(
    ds: ReclaimableDataset, *, retention_days: int | None, retain_versions: int | None, protected: BaseRefs | None = None, branch: str | None = None
) -> GcRunData:
    """Reclaim old versions (DESTRUCTIVE). Tagged versions are exempt, exactly like the compaction sweep.

    THE STEP THAT ACTUALLY DELETES, which is why ``protected`` matters most here: measured, compaction
    adds the merged file and removes nothing, and it is this call that then removes the obsoleted
    originals a shallow clone still resolves through. See :func:`require_reclaimable`, which gates
    this door on the sweep's own root-scoped gate — the same one the cron applies to the same dataset
    every tick. ``branch`` is the ref ``ds`` opens, for :func:`refuse_a_reclaim_over_nested_branches`.
    """
    require_reclaimable(ds, protected)
    refuse_a_reclaim_over_nested_branches(ds, branch)
    older_than = timedelta(days=retention_days) if retention_days else timedelta(0)
    stats: Any = ds.cleanup_old_versions(older_than=older_than, retain_versions=retain_versions, error_if_tagged_old_versions=False)
    return {
        "ok": True,
        "old_versions_removed": int(getattr(stats, "old_versions", 0) or 0),
        "bytes_removed": int(getattr(stats, "bytes_removed", 0) or 0),
    }


#: ``VersionRange.end_version`` for "through the latest version" (``lance_docs/ns_catalog/spec.yaml``).
THROUGH_LATEST: Final = -1


def versions_in_ranges(ranges: Sequence[tuple[int, int]], *, current: int) -> list[int]:
    """The versions ``ranges`` name, each ``(start, end)`` start-inclusive and end-exclusive.

    A range that reaches ``current`` is refused, the spec's ``(0, -1)`` "all versions" included: Lance
    commits a version by put-if-not-exists on its number, so deleting the latest manifest rolls the
    table back and the next write mints that number again. Every range is judged before any is applied.
    """
    if not ranges:
        raise InvalidInputError("version/delete names no version range")
    named: set[int] = set()
    for start, end in ranges:
        if end == THROUGH_LATEST or end > current:
            raise InvalidInputError(
                f"range [{start}, {end}) reaches version {current}, the current version: deleting it lets the next write mint that "
                "number again, so this door never does. end_version is exclusive; nothing was deleted"
            )
        if start < 0 or end <= start:
            raise InvalidInputError(f"range [{start}, {end}) names no version: start_version is inclusive, end_version exclusive; nothing was deleted")
        named.update(range(start, end))
    return sorted(named)


def _refuse_pinned_versions(ds: VersionedDataset, branch: str | None, targets: Sequence[int]) -> None:
    """Refuse when a tag on this ref, or a branch cut from it, pins one of ``targets`` — naming every pin."""
    wanted = set(targets)
    pins = [f"tag {name!r} at version {version}" for name, version in tag_versions(ds, branch).items() if version in wanted]
    pins += [f"branch {name!r} cut at version {version}" for name, version in fork_versions(ds, branch).items() if version in wanted]
    if pins:
        raise InvalidTableStateError(f"refused: {', '.join(sorted(pins))} pins a version in the range; delete the pin first. Nothing was deleted")


def delete_versions(ds: VersionDeletableDataset, *, ranges: Sequence[tuple[int, int]], branch: str | None, protected: BaseRefs | None) -> int:
    """Delete the versions ``ranges`` name on ``ds``'s ref (DESTRUCTIVE); return how many were removed.

    ``cleanup_old_versions(versions=...)``, measured on pylance 12.0.0: the current version is never
    removed, a tagged one raises under ``error_if_tagged_old_versions=True``, a version a branch was cut
    from is kept, and data files only the removed versions referenced go with them. A pin is refused
    before the call so the caller learns which one; a tag that lands between that check and the call is
    refused by the call and named from a second read. A tag or branch created while the call runs is
    not seen: Lance reads the refs once, and nothing serializes ref creation with a reclaim (measured
    with 60 concurrent races: a tag named a deleted version in 31, and ``run_gc`` loses the same race).
    ``protected`` is the #114 base-reference map, as for ``run_gc``.
    """
    targets = versions_in_ranges(ranges, current=int(ds.version))
    require_reclaimable(ds, protected)
    refuse_a_reclaim_over_nested_branches(ds, branch)
    _refuse_pinned_versions(ds, branch, targets)
    try:
        stats: Any = ds.cleanup_old_versions(versions=targets, error_if_tagged_old_versions=True)
    except OSError:
        _refuse_pinned_versions(ds, branch, targets)
        raise
    return int(getattr(stats, "old_versions", 0) or 0)
