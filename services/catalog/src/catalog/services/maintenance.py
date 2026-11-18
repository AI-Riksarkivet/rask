"""#75 on-demand garbage collection — the operator's per-table analog of the maintenance sweep's version expiry.

``preview_gc`` is a DRY RUN: which old versions ``cleanup_old_versions`` would reclaim, honouring the
current version, the pins on THIS ref (a version tagged on this branch, or one a child branch was cut
from, is NEVER collected), the retain-last-N window, and the age cutoff — it never mutates. ``run_gc``
performs the reclaim with the SAME tag exemption the sweep uses (``error_if_tagged_old_versions=False``),
so a long-lived tag can't stall GC. ``delete_versions`` backs the spec's version-delete door: it condemns the named
versions and deletes nothing, and refuses a pinned one instead of skipping it. Pure over a Lance dataset handle, so these
are unit-testable with a fake ``ds``.

The reclaims are gated by :func:`require_reclaimable`, the sweep's own root-scoped gate. :func:`require_compactable`
is the layout gate a compaction a client stages is judged against at ``version/create``, the one maintenance asks before
it lists a table as due for compaction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, TypedDict, cast

from lance_namespace import InvalidInputError, InvalidTableStateError, ServiceUnavailableError, UnsupportedOperationError

from lakehouse import branch_layout, condemned_versions
from lakehouse.compaction_gate import compaction_refusal
from lakehouse.features import FragmentCarrier, ManifestCarrier, describe_gc_unsupported_flags, manifest_feature_flags
from lakehouse.version_retention import ExplainedDataset, fork_versions, reclaimable_versions, recorded_branch, tag_versions


if TYPE_CHECKING:
    from lakehouse.condemned_versions import Verdict
    from lakehouse.storage_options import StorageOptions

#: How a reclaim asks the catalog's batch for each group of versions (``catalog.services.reclaims.condemner``).
type Condemn = Callable[[list[int]], Verdict]


log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# The dataset surface these doors need, stated (CAT-CORE-15). Structural, not nominal
# `lance.LanceDataset`: the module docstring promises these are "pure over a Lance dataset handle, so
# both are unit-testable with a fake ``ds``", and a nominal type would make that promise unkeepable.
# Split per verb, mirroring `lakehouse.features`' ManifestCarrier/FragmentCarrier: a door
# that only previews must not claim it can reclaim.
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


class PreviewedDataset(VersionedDataset, ExplainedDataset, Protocol):
    """The preview surface PLUS Lance's explanation of the cleanup the run would make."""


class ReclaimableDataset(VersionedDataset, ManifestCarrier, Protocol):
    """The preview surface PLUS the destructive reclaim — and the manifest the gate reads first."""

    def cleanup_old_versions(self, *, older_than: timedelta, retain_versions: int | None, error_if_tagged_old_versions: bool) -> Any: ...  # noqa: ANN401 — pylance returns an untyped stats object


class VersionDeletableDataset(VersionedDataset, ManifestCarrier, Protocol):
    """The preview surface PLUS the reclaim of NAMED versions — and the manifest the gate reads first."""

    def cleanup_old_versions(self, *, versions: list[int], error_if_tagged_old_versions: bool) -> Any: ...  # noqa: ANN401 — pylance returns an untyped stats object


def require_compactable(ds: FragmentCarrier, table_location: str, storage_options: StorageOptions) -> None:
    """Refuse compacting a dataset whose layout makes a rewrite copy bytes it does not own (#121).

    The gate maintenance asks before it lists a table as due for compaction, one function
    (:func:`~lakehouse.compaction_gate.compaction_refusal`), so a compaction is judged by the evidence that
    listed it: :func:`~lakehouse.features.describe_compaction_unsupported_flags` over three readings
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


def require_reclaimable(ds: ManifestCarrier) -> None:
    """#121 for the GC door: refuse a dataset whose versions this button must not reclaim.

    **THE GATE IS THE SWEEP'S, per verb** — :func:`~lakehouse.features.describe_gc_unsupported_flags`,
    which is what ``maintenance.services.expiry`` asks before its own ``cleanup_old_versions``.
    Version reclamation and index maintenance are ROOT-SCOPED, so ``base_paths`` (16) does not
    endanger them: measured on pylance 9.0.0 across six cleanup shapes and ten repeat cycles, one
    ``cleanup_old_versions`` on a clone with dead fragments on both sides removed the 2 clone-owned
    files, left all 4 base-owned ones, and the base still read in a fresh process. Everything else —
    flag 64, anything unknown — refuses exactly as before.

    A registered clone of this table needs no refusal here: its tag keeps the version it reads, and every cleanup
    exempts a tagged version (ADR 0198 §1, :mod:`lakehouse.clone_sources`).
    """
    reader, writer = manifest_feature_flags(ds)
    if (reason := describe_gc_unsupported_flags(reader, writer)) is not None:
        raise UnsupportedOperationError(
            f"maintenance refused: {reason}. Reclaiming versions here would act on a layout this pass cannot correctly rewrite — "
            "the sweep's version-reclamation gate refuses it too."
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


def preview_gc(ds: PreviewedDataset, *, branch: str | None, retention_days: int | None, retain_versions: int | None) -> GcPreviewData:
    """Dry-run the old-version cleanup — the versions GC would reclaim, and the tags protecting others.

    ``branch`` names the ref ``ds`` is checked out on, and it is REQUIRED because the handle cannot say:
    pylance exposes no current-branch accessor, and ``tags.list()`` / ``branches.list()`` answer the
    same root-scoped maps from every ref. Which of their pins protect a version depends on it.
    """
    current = int(ds.version)
    # The run's own arguments (:func:`run_gc`), so the versions offered are Lance's answer to the cleanup it makes.
    eligible = reclaimable_versions(ds, older_than=_age_bound(retention_days), retain_versions=retain_versions)
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


def _age_bound(retention_days: int | None) -> timedelta:
    """The age a reclaimed version must exceed: ``retention_days``, or any age when none is named."""
    return timedelta(days=retention_days) if retention_days else timedelta(0)


def run_gc(ds: ReclaimableDataset, *, retention_days: int | None, retain_versions: int | None, branch: str | None, condemn: Condemn) -> GcRunData:
    """Reclaim old versions (DESTRUCTIVE). Tagged versions are exempt, as in maintenance's own expiry.

    Tagged versions are what keeps a registered clone readable: its tag on this table pins the version it reads
    (ADR 0198 §1). See :func:`require_reclaimable`, which gates this door on maintenance's own root-scoped gate.
    ``branch`` is the ref ``ds`` opens, for :func:`refuse_a_reclaim_over_nested_branches`. ``condemn`` asks the catalog's
    batch of each 16 versions (:func:`lakehouse.condemned_versions.reclaim`, ADR 0246), which the one reclaim every cleanup
    goes through deletes only what it licenses.

    Raises:
        InvalidTableStateError: A hold at a level that can govern the table keeps its whole history (19); nothing was
            deleted.
        ServiceUnavailableError: The store refused a read or a delete (503, retryable).
    """
    require_reclaimable(ds)
    refuse_a_reclaim_over_nested_branches(ds, branch)
    with _a_store_refusal_retried():
        victims = condemned_versions.victims(cast("Any", ds), older_than=_age_bound(retention_days), retain_versions=retain_versions)
        done = condemned_versions.reclaim(cast("Any", ds), victims, condemn)
    if done.held_by is not None:
        raise InvalidTableStateError(
            f"a maintenance policy holds the table's whole history ({done.held_by}: cleanup_enabled false), so no version is deleted; lift the hold "
            "at its policy door first. Nothing was deleted"
        )
    _refuse_a_refused_delete(done.failed_deletes, removed=done.old_versions)
    return {
        "ok": True,
        "old_versions_removed": done.old_versions,
        "bytes_removed": done.bytes_removed,
    }


@contextmanager
def _a_store_refusal_retried() -> Generator[None]:
    """A reclaim the store refused a read or a delete of, as the retryable refusal it is: Lance raises its refusal as an
    IO ``ValueError`` or ``OSError`` (measured on RustFS 1.0.3: ``ExpiredToken`` on a cleanup's first listing). Nothing
    more was deleted, and the batch's versions stay condemned.

    Raises:
        ServiceUnavailableError: Lance's cleanup failed on the store (503, retryable).
    """
    try:
        yield
    except (OSError, ValueError) as exc:
        if "LanceError(IO)" not in str(exc):
            raise
        raise ServiceUnavailableError(
            "the store refused this reclaim's reads or deletes, so nothing more was deleted and the versions it was removing stay condemned, which "
            "no tag, branch, restore or clone names: retry"
        ) from exc


def _refuse_a_refused_delete(failed: int, *, removed: int) -> None:
    """A reclaim the store refused a delete of is no success: Lance counts a refused delete and returns
    (``CleanupStats.failed_deletes``, measured on pylance 13.0.0), and its versions stay condemned, so no door names them
    until a later reclaim finishes them (KeepAfterDelete, ADR 0246).

    Raises:
        ServiceUnavailableError: ``failed`` is not 0 (503, retryable).
    """
    if failed:
        raise ServiceUnavailableError(
            f"the store refused {failed} of this reclaim's deletes after {removed} versions were removed; the versions it was removing stay "
            "condemned, so no tag, branch, restore or clone names them, and the next reclaim finishes them: retry"
        )


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


def delete_versions(ds: VersionDeletableDataset, *, ranges: Sequence[tuple[int, int]], branch: str | None, condemn: Callable[[list[int]], list[int]]) -> int:
    """Delete the version records ``ranges`` name on ``ds``'s ref; return how many were condemned.

    The spec's operation (``lance_docs/ns_catalog/spec.yaml``, BatchDeleteTableVersions: "deletes version tracking records,
    NOT the actual table data"): ``condemn`` writes each version into the table's condemned versions
    (:func:`lakehouse.condemned_versions.condemn_named`), which ``version/list`` leaves out and every ref door refuses
    from then on, and deletes nothing; the next reclaim of the ref finishes them unless a hold keeps them (ADR 0246). A
    version a tag or a branch pins is refused before anything is condemned, naming the pin, and a version a registered
    clone reads is pinned by the clone's tag, so it is refused like any tagged one (ADR 0198 §1).
    """
    targets = versions_in_ranges(ranges, current=int(ds.version))
    require_reclaimable(ds)
    refuse_a_reclaim_over_nested_branches(ds, branch)
    _refuse_pinned_versions(ds, branch, targets)
    return len(condemn(targets))
