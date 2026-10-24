"""Version expiry: one dataset's superseded versions reclaimed, behind the refusal ladder that guards reclamation.

Maintenance decides and does not rewrite (ADR 0155, ADR 0156 C4): it compacts nothing and builds or optimizes no
index. What it still writes is reclamation — ``cleanup_old_versions`` deletes superseded manifests and the files only
they reference, the pass deletes the manifests stock writers staged for versions another commit took, and what no
retained version references in the table's own data directories (:mod:`maintenance.services.base_reclaim`) — and the
config commit that disarms Lance's commit-path auto-cleanup. Every gate here guards one of those writes.
"""

from __future__ import annotations

import logging
import re
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import lance
from pydantic import BaseModel, ConfigDict, Field

from maintenance.core.config import shared_lance_session
from maintenance.core.metrics import CatalogRefusal
from maintenance.services import base_reclaim
from service_kit.lakehouse import auto_cleanup
from service_kit.lakehouse.base_refs import BaseRefs, rewrite_refusal
from service_kit.lakehouse.base_registry import RecordedBase
from service_kit.lakehouse.branch_layout import BRANCH_CONTAINER, branch_named_by, branches_inside
from service_kit.lakehouse.features import (
    describe_gc_unsupported_flags,
    flags_from_open_error,
    manifest_feature_flags,
    mixes_data_file_versions,
    unsupported_features_from_open_error,
)
from service_kit.lakehouse.history_fences import HistoryFencedError, HistoryWork, TableHistory
from service_kit.lakehouse.stored_objects import delete_objects, list_under
from service_kit.lakehouse.version_retention import Retention, reclaimable_versions
from service_kit.openlineage import DatasetUuids


log = logging.getLogger(__name__)

#: A refusal is "we declined, before touching a byte" — never a skip ("not this tick") and never an error ("something
#: failed"). The gate names which: a count that merges them is not actionable, because a clone's source stays refused
#: forever, a manifest flag is a pylance upgrade away from being supported, and a bad branch name clears when somebody
#: removes a directory. A closed set: it is a metric label.
type Gate = Literal["protected_base", "manifest_flags", "invalid_ref", "nested_branch", "governed_elsewhere", "unauthenticated", "unnamed", "unidentified"]

#: Whether the dataset's table needs compacting or reindexing, as this tick's due evaluation decided
#: (:mod:`maintenance.services.due`): ``due`` wrote its record, ``clear`` deleted it, ``unnamed`` found no
#: ``__manifest`` row naming the dataset, ``failed`` could not decide and left the record as it was.
type DueVerdict = Literal["due", "clear", "unnamed", "failed"]

#: Why a pass that would delete did not run this tick: a write of the table's tags or branches held its history ([[LH-308]]).
HISTORY_FENCED: Final = "history_fenced"


class DatasetResult(BaseModel):
    """What one dataset's pass did, or why it did not run."""

    uri: str
    #: Why this dataset's pass did not run this tick: the policy cadence (``policy_interval``), or a write of one of the
    #: table's tags or branches holding its history ([[LH-308]], :data:`HISTORY_FENCED`); ``None`` when the pass ran.
    skipped: str | None = None
    #: Why this dataset was REFUSED, and by which gate (:data:`Gate`, or one of the catalog's answers for the id).
    refused: str | None = None
    refused_by: Gate | CatalogRefusal | None = None
    #: The id the catalog refused, on one of its refusals: what `compaction.tables.parked` labels.
    refused_table_id: str | None = None
    #: The table's ``data_storage_version`` as its manifest declares it; ``None`` when the manifest was not read.
    data_storage_version: str | None = None
    #: The manifest carries reader flag 256: its data files are at more than one file version. Sticky, and the table
    #: is unopenable by pylance 11 and lancedb 0.34, so it is counted on its own beside any refusal.
    mixed_data_file_versions: bool = False
    #: F6(d): this dataset is in the TRASH, recoverable until its deadline and therefore frozen. Names the record id and
    #: its deadline, so a record stuck long past its deadline reads as a permanent exclusion.
    trashed: str | None = None
    #: The versions the policy would reclaim when the pass looked, ``None`` when it did not count them.
    reclaimable_versions: int | None = None
    old_versions_removed: int = 0
    bytes_removed: int = 0
    #: Manifests stock writers staged for versions the ref already holds, removed by this pass ([[LH-529]]).
    staged_manifests_removed: int = 0
    #: Files in the table's own data directories that no retained version references, removed by this pass ([[LH-286]]).
    base_files_removed: int = 0
    base_bytes_removed: int = 0
    #: The table's fragments that keep a deleted row's bytes in one of its data directories: only a compaction rewrites
    #: them, and the compaction gate refuses such a table ([[LH-286]]).
    base_fragments_holding_deleted_rows: int = 0
    #: This pass found Lance's commit-path auto-cleanup armed and removed every ``lance.auto_cleanup.*`` key
    #: ([[LH-245]]).
    auto_cleanup_disarmed: bool = False
    #: The table this dataset is, or whose branch it is, as the catalog's ``__manifest`` names it; ``None`` when none does.
    table_id: str | None = None
    #: The uuids of that table and its namespace, read for a pass whose lineage event names them (ADR 0173 §10).
    uuids: DatasetUuids = Field(default_factory=DatasetUuids)
    due: DueVerdict | None = None
    error: str | None = None
    #: The exception CLASS name, never the message: a stable identifier for span aggregation.
    error_type: str | None = None
    #: Wall-clock seconds the pass took, or ``None`` when nothing was attempted ([[LH-098]]).
    duration_seconds: float | None = None

    def park(self, reason: str, *, gate: CatalogRefusal, table_id: str | None) -> None:
        """Record a catalog refusal for this dataset's table id: nothing more is done to it this tick."""
        self.refused = reason
        self.refused_by = gate
        self.refused_table_id = table_id

    def refuse(self, reason: str, *, gate: Gate) -> DatasetResult:
        """This result, refused by ``gate``."""
        self.refused = reason
        self.refused_by = gate
        return self


class ExpiryPolicy(BaseModel):
    """The retention one dataset is expired under, resolved from the winning policy record."""

    #: Versions older than this may go; ``None`` with ``retain_versions`` set is pure keep-last-N. Never both ``None``:
    #: pylance then substitutes a 14-day default.
    older_than: timedelta | None
    retain_versions: int | None = None
    #: ``False`` keeps the ENTIRE version history: a tier under legal hold, or one whose time-travel window is the product.
    cleanup_enabled: bool = True
    #: Reclaim only once at least this many versions are reclaimable (``MAINTENANCE_MIN_VERSIONS_TO_EXPIRE``).
    min_reclaimable: int = Field(default=1, ge=1)
    #: How long a manifest staged for a version the ref already holds is kept before the pass removes it
    #: (``MAINTENANCE_STAGED_MANIFEST_GRACE_SECONDS``): past any commit still deciding its outcome.
    staged_grace: timedelta = timedelta(minutes=10)

    def retention(self) -> Retention:
        return Retention(older_than=self.older_than, retain_versions=self.retain_versions)


class ExpiryTarget(BaseModel):
    """The dataset an expiry pass runs on, and what the sweep already knows about it."""

    uri: str
    #: The credential the pass signs with: vended and table-scoped where the catalog offers one.
    storage_options: dict[str, str]
    #: The whole-estate base references this tick's pre-pass took.
    protected: BaseRefs = Field(default_factory=BaseRefs)
    #: The id the caller resolved, and vended the credential under.
    table_id: str | None = None
    #: The table's own data directories, as its base record names them; empty for a branch, whose files are its table's.
    data_directories: list[RecordedBase] = Field(default_factory=list)
    #: The tick's clock, which a file's age in a data directory is judged by.
    now: datetime = Field(default_factory=lambda: datetime.now(UTC))
    #: Where the table's history fence is kept, which the pass holds while it deletes ([[LH-308]]); ``None`` holds none.
    history: TableHistory | None = None


class DisarmOutcome(BaseModel):
    """What :func:`_disarm_commit_path_cleanup` did, carried onto whichever result the pass returns."""

    disarmed: bool = False
    error: str | None = None
    error_type: str | None = None

    def onto(self, result: DatasetResult) -> DatasetResult:
        result.auto_cleanup_disarmed = self.disarmed
        if self.error is not None and result.error is None:
            result.error, result.error_type = self.error, self.error_type
        return result


class _Opened(BaseModel):
    """A dataset opened for expiry, with the manifest facts every outcome reports."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ds: lance.LanceDataset
    reader_flags: int
    writer_flags: int
    data_storage_version: str | None
    mixed: bool

    def result(self, uri: str) -> DatasetResult:
        return DatasetResult(uri=uri, data_storage_version=self.data_storage_version, mixed_data_file_versions=self.mixed)


def _disarm_commit_path_cleanup(ds: lance.LanceDataset, *, uri: str) -> DisarmOutcome:
    """Remove every ``lance.auto_cleanup.*`` key, the backstop for a table no door commits on ([[LH-245]]).

    Every catalog door that commits disarms the ref first (:mod:`service_kit.lakehouse.auto_cleanup` records the
    measurements); a table nobody commits on is disarmed here, so it does not stay armed until some later commit
    reclaims behind the sweep's gates.

    Called right after the open, BEFORE every refusal gate: the hold and the protected base are exactly the tables
    whose history the lane would delete. The write is a config-only commit, and only when a key is present. A failure
    is reported on the result, never raised: the dataset stays armed, which an operator must see, and the next tick
    retries.
    """
    try:
        armed = auto_cleanup.disarm(ds)
    except Exception as exc:  # noqa: BLE001 — reported on the result; the pass continues
        log.warning("auto_cleanup_disarm_failed", extra={"uri": uri, "error": str(exc)})
        return DisarmOutcome(error=f"auto_cleanup: {exc}", error_type=type(exc).__name__)
    if not armed:
        return DisarmOutcome()
    log.warning("auto_cleanup_disarmed", extra={"uri": uri, "keys": armed})
    return DisarmOutcome(disarmed=True)


#: The prefix pylance puts on every ref-name rejection. Matched as a PREFIX: the bodies are upstream's wording, and a
#: list of them would silently stop matching the day upstream rephrases one.
_INVALID_REF = "Ref is invalid:"


def classify_maintain_failure(message: str) -> Literal["invalid_ref"] | None:
    """The refusal gate a pass failure belongs to, or ``None`` when it is a real failure.

    A ref whose NAME Lance will not parse can never be maintained — no retry, no upgrade and no action on this pass
    changes it — so it is a refusal, not a failure nothing can clear. Measured on the live estate 2026-09-20: eleven
    such datasets across three parents, all branch directories under `<dataset>/tree/`. NARROW ON PURPOSE: classifying
    too broadly would file genuine faults as "declined".
    """
    return "invalid_ref" if message.startswith(_INVALID_REF) else None


def summarize_refusals(results: list[DatasetResult]) -> dict[str, int]:
    """How many datasets each gate refused this sweep — empty when none were, so a caller can stay silent."""
    counts: dict[str, int] = {}
    for result in results:
        if result.refused_by is not None:
            counts[result.refused_by] = counts.get(result.refused_by, 0) + 1
    return counts


def _branches_inside(ds: lance.LanceDataset, uri: str) -> list[str]:
    """The branches whose directory lies inside the one ``uri`` opens, when ``uri`` is a branch ([[LH-203]]).

    A dataset opened at ``<root>/tree/a`` lists what lies under ``tree/a/_versions/`` as its own versions, so branch
    ``a/_versions`` reads as part of ``a``'s history: measured on pylance 12.0.0, ``cleanup_old_versions`` through
    ``tree/a`` deleted that branch's manifests and a cold read of it failed ``Not found``. ``branches.list()`` answers
    the ROOT's refs from a branch handle too (measured), so one read gives both the name ``uri`` is and the names
    under it.
    """
    if f"/{BRANCH_CONTAINER}/" not in uri:
        return []
    names = list(ds.branches.list())
    own = branch_named_by(uri, names)
    return [] if own is None else branches_inside(own, names)


def _refuse_a_branch_with_branches_inside(opened: _Opened, uri: str) -> DatasetResult | None:
    """The ``nested_branch`` refusal for ``uri``, an error when its refs cannot be read, or ``None`` to proceed."""
    try:
        nested = _branches_inside(opened.ds, uri)
    except Exception as exc:
        failed = opened.result(uri)
        failed.error, failed.error_type = f"refs: {exc}", type(exc).__name__
        return failed
    if not nested:
        return None
    log.warning("maintenance_refused_nested_branch", extra={"uri": uri, "nested": nested})
    why = f"branches {nested} lie inside this branch's directory; Lance reads their files as this branch's, so reclaiming here would delete them"
    return opened.result(uri).refuse(why, gate="nested_branch")


def _refuse_unsupported_flags(opened: _Opened, uri: str) -> DatasetResult | None:
    """The ``manifest_flags`` refusal when the manifest sets a feature reclamation cannot handle (#64), else ``None``.

    The mask is ``SUPPORTED_FOR_GC``, which admits flag 16: ``cleanup_old_versions`` is root-scoped, so it removes a
    clone's own dead files and leaves the base's alone (measured on pylance 9.0.0, the base still read in a fresh
    process).
    """
    refusal = describe_gc_unsupported_flags(opened.reader_flags, opened.writer_flags)
    if refusal is None:
        return None
    log.warning("maintenance_refused_unsupported_features", extra={"uri": uri, "reason": refusal})
    return opened.result(uri).refuse(refusal, gate="manifest_flags")


def _refuse_a_referenced_root(opened: _Opened, target: ExpiryTarget) -> DatasetResult | None:
    """The ``protected_base`` refusal when another dataset resolves its files through this one (#114), else ``None``.

    The source of a shallow clone carries no flag and no ``base_paths`` of its own; only the whole-estate pre-pass
    knows another manifest resolves through these bytes. Measured: ``cleanup_old_versions`` after a rewrite removed the
    obsoleted originals and the clone failed to open IN A FRESH PROCESS. The rule is
    :func:`service_kit.lakehouse.base_refs.rewrite_refusal`, which permits an in-root branch and a root held by its
    pins. DEBUG, not WARNING: measured 2026-09-18, the line fired 116 times in ONE tick for the same permanent facts;
    the sweep's summary reports the count and the breakdown once.
    """
    if target.protected.is_protected(target.uri) is None:
        return None
    why = rewrite_refusal(target.protected, target.uri, opened.ds.tags.list())
    if why is None:
        return None
    log.debug("maintenance_refused_protected_base", extra={"uri": target.uri, "reason": why})
    return opened.result(target.uri).refuse(why, gate="protected_base")


def _open(target: ExpiryTarget) -> _Opened | DatasetResult:
    """The dataset opened with its manifest facts, or the result that says why it could not be.

    pylance refuses a manifest whose flags IT does not know before they can be read here (measured: a committed data
    overlay, flag 64). That is a REFUSAL, not an unopenable directory: reported as `open:` it reads as transient noise
    and the lineage layer drops it.
    """
    try:
        ds = lance.dataset(target.uri, storage_options=target.storage_options, session=shared_lance_session())
        reader_flags, writer_flags = manifest_feature_flags(ds)
        return _Opened(
            ds=ds,
            reader_flags=reader_flags,
            writer_flags=writer_flags,
            data_storage_version=ds.data_storage_version,
            mixed=mixes_data_file_versions(reader_flags),
        )
    except Exception as exc:
        if (refusal := unsupported_features_from_open_error(exc)) is None:
            return DatasetResult(uri=target.uri, error=f"open: {exc}", error_type=type(exc).__name__)
        log.warning("maintenance_refused_unsupported_features", extra={"uri": target.uri, "reason": refusal})
        refused = DatasetResult(uri=target.uri, mixed_data_file_versions=mixes_data_file_versions(flags_from_open_error(exc) or 0))
        return refused.refuse(refusal, gate="manifest_flags")


def _refusal(opened: _Opened, target: ExpiryTarget) -> DatasetResult | None:
    """The first gate that refuses reclamation on this dataset, in ladder order, or ``None`` to reclaim."""
    return (
        _refuse_a_branch_with_branches_inside(opened, target.uri) or _refuse_unsupported_flags(opened, target.uri) or _refuse_a_referenced_root(opened, target)
    )


def count_reclaimable(ds: lance.LanceDataset, uri: str, policy: ExpiryPolicy) -> int:
    """How many versions ``policy`` would reclaim on the ref ``uri`` opens: the catalog's GC-preview rule."""
    branch = branch_named_by(uri, list(ds.branches.list())) if f"/{BRANCH_CONTAINER}/" in uri else None
    return len(reclaimable_versions(ds, branch=branch, retention=policy.retention(), now=datetime.now(UTC)))


#: A manifest a stock writer stages, `<inverted version>.manifest-<uuid>` (ADR 0157). Lance's listing drops the name
#: (`detect_scheme`, `rust/lance-table/src/io/commit.rs:138-150` at v13.0.0), so ``cleanup_old_versions`` never reclaims
#: one.
_STAGED_MANIFEST: Final = re.compile(r"^(?P<inverted>\d{20})\.manifest-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_U64_MAX: Final = (1 << 64) - 1


def _staged_manifests(uri: str, storage_options: dict[str, str]) -> list[tuple[str, int, datetime]]:
    """Each manifest staged under the ref's ``_versions/``: its store path, the version it was staged for, when it was put."""
    staged: list[tuple[str, int, datetime]] = []
    for listed in list_under(f"{uri.rstrip('/')}/_versions", storage_options):
        if (match := _STAGED_MANIFEST.match(listed.path.rsplit("/", 1)[-1])) is not None:
            staged.append((listed.path, _U64_MAX - int(match["inverted"]), listed.modified))
    return staged


def _reclaim_staged_manifests(ds: lance.LanceDataset, *, result: DatasetResult, target: ExpiryTarget, policy: ExpiryPolicy) -> None:
    """Remove each manifest staged for a version the ref already holds, once past ``policy.staged_grace`` ([[LH-529]]).

    A commit that loses its version to another leaves the manifest it staged: Lance deletes a losing one with the
    committer's own credential, which the write tier does not grant, and keeps one whose outcome it cannot prove
    (`lose_or_retain`, `rust/lance-table/src/io/commit/external_manifest.rs` at v13.0.0). Neither can land once another
    commit holds its version, and each is one more key every open of the table lists. One staged for a later version is
    a commit still in flight and is kept, and the grace keeps a commit deciding its outcome from losing the file.
    """
    cutoff = datetime.now(UTC) - policy.staged_grace
    lost = [path for path, version, modified in _staged_manifests(target.uri, target.storage_options) if version <= ds.version and modified <= cutoff]
    delete_objects(target.uri, target.storage_options, lost)
    result.staged_manifests_removed = len(lost)


def _reclaim_versions(ds: lance.LanceDataset, *, result: DatasetResult, target: ExpiryTarget, policy: ExpiryPolicy) -> None:
    """Reclaim superseded versions once enough are reclaimable, then the manifests no version will take; tagged versions
    are EXEMPT.

    ``error_if_tagged_old_versions=False``: the default RAISES once any tag ages past ``older_than``, and the catalog's
    long-lived ``published`` tag would then stall reclamation on that dataset forever. The staged manifests go in the same
    pass because only a pass that reclaims holds a write credential (``sweep._probe_before_vending``).
    """
    if not policy.cleanup_enabled:
        log.info("cleanup_disabled_by_policy", extra={"uri": result.uri})
        return
    if (result.reclaimable_versions or 0) < policy.min_reclaimable:
        log.debug("maintenance_expiry_below_threshold", extra={"uri": result.uri, "reclaimable": result.reclaimable_versions})
        return
    stats: Any = ds.cleanup_old_versions(older_than=policy.older_than, retain_versions=policy.retain_versions, error_if_tagged_old_versions=False)
    result.old_versions_removed = int(getattr(stats, "old_versions", 0))
    result.bytes_removed = int(getattr(stats, "bytes_removed", 0))
    _reclaim_staged_manifests(ds, result=result, target=target, policy=policy)


def _reclaim_data_directories(ds: lance.LanceDataset, *, result: DatasetResult, target: ExpiryTarget, policy: ExpiryPolicy) -> None:
    """Reclaim what no retained version references in the table's own data directories, and count what a deleted row keeps.

    After ``cleanup_old_versions``, which deletes the manifests and the root's files and leaves every base alone, so the
    versions it removed no longer hold a base file here ([[LH-286]]). Whatever the reclaimable-version count, because a
    base file a past pass found too young waits for a later one. Never under a policy that disables cleanup: a table
    whose whole history is kept keeps every file that history could name.
    """
    if not target.data_directories:
        return
    result.base_fragments_holding_deleted_rows = base_reclaim.fragments_holding_deleted_rows(ds, target.data_directories)
    if not policy.cleanup_enabled:
        return
    reclaimed = base_reclaim.reclaim(ds, target.data_directories, target.storage_options, now=target.now)
    result.base_files_removed, result.base_bytes_removed = reclaimed.files, reclaimed.bytes


def _deletes(result: DatasetResult, target: ExpiryTarget, policy: ExpiryPolicy) -> bool:
    """Whether this pass will delete: versions past the policy's threshold, or files in the table's data directories."""
    return policy.cleanup_enabled and ((result.reclaimable_versions or 0) >= policy.min_reclaimable or bool(target.data_directories))


def _history_held(result: DatasetResult, target: ExpiryTarget, policy: ExpiryPolicy) -> AbstractContextManager[None]:
    """The table's history held for a reclaim while a pass that deletes runs ([[LH-308]]); nothing held for one that does not.

    ``cleanup_old_versions`` reads the table's tags and branches once, when it starts, so a ref written while it runs can
    name a version it deletes. Only a pass that deletes holds the fence, so a table's ref writes wait on no pass that
    reclaims nothing.

    Raises:
        HistoryFencedError: A write of the table's tags or branches holds its history.
    """
    if target.history is None or not _deletes(result, target, policy):
        return nullcontext()
    return target.history.held(HistoryWork.RECLAIM)


def _record_failure(result: DatasetResult, exc: BaseException) -> None:
    """A reclaim that raised: a ref Lance will never accept is a refusal, anything else a failure."""
    if (gate := classify_maintain_failure(str(exc))) is not None:
        log.warning("maintenance_ref_unmaintainable", extra={"uri": result.uri, "reason": str(exc)})
        result.refuse(str(exc), gate=gate)
        return
    result.error = f"maintain: {exc}"
    result.error_type = type(exc).__name__


def expire_versions(target: ExpiryTarget, policy: ExpiryPolicy) -> DatasetResult:
    """One dataset's version expiry. Never raises: a per-dataset failure is captured in ``error``.

    Opens the dataset, disarms commit-path auto-cleanup before any gate can return (version reclamation has ONE owner,
    this pass), runs the refusal ladder that guards reclamation, then reclaims the versions and what they left in the
    table's data directories.
    """
    opened = _open(target)
    if isinstance(opened, DatasetResult):
        return opened
    disarm = _disarm_commit_path_cleanup(opened.ds, uri=target.uri)
    if (refused := _refusal(opened, target)) is not None:
        return disarm.onto(refused)
    result = disarm.onto(opened.result(target.uri))
    result.table_id = target.table_id
    try:
        if policy.cleanup_enabled:
            result.reclaimable_versions = count_reclaimable(opened.ds, result.uri, policy)
        with _history_held(result, target, policy):
            _reclaim_versions(opened.ds, result=result, target=target, policy=policy)
            _reclaim_data_directories(opened.ds, result=result, target=target, policy=policy)
    except HistoryFencedError as exc:
        log.info("maintenance_history_fenced", extra={"uri": result.uri, "table_id": result.table_id, "reason": str(exc)})
        result.skipped = HISTORY_FENCED
    except BaseException as exc:  # noqa: BLE001 — a Rust PANIC is not an Exception
        # A pyo3 panic derives from BaseException; measured on the live estate 2026-08-16, one escaping panic answered
        # the entire sweep HTTP 500 and every other dataset lost its reclamation.
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        _record_failure(result, exc)
    return result
