"""Unreferenced-file detection — the reclamation gap, REPORTED and never acted on.

**NOTHING DELETES. NOTHING MUTATES.** Every path here is read-only. An orphan reclaimer that deletes
on its first run against a rule nobody has validated is how a maintenance job eats live data, so this
produces a report a later, separately-decided reclaimer can consume once the report has run clean on
a real estate.

`cleanup_old_versions` reclaims OLD VERSIONS. It does not reclaim files that no version references at
all, and nothing else does either — the phrase "remove orphans" appeared nowhere in this service.

What can be unreferenced, per the Lance table spec (lance.org/format/table/layout/ and /transaction/,
fetched 2026-08-04):

* `data/*.lance` files listed in no live manifest;
* `_deletions/*` vectors no fragment references;
* `_indices/<uuid>/` directories absent from every live manifest;
* `_transactions/*.txn` files from failed or rolled-back commits — **these accumulate BY DESIGN**:
  on a conflict "transaction files remain in storage describing each commit attempt", so a busy table
  grows them forever and nothing prunes them;
* manifests of versions that were deleted;
* `_versions/latest_version_hint.json`, which the spec calls "purely an optimization" and "always
  safe to delete".

Plus rask's own two producers: a partially-failed write (fragments written, commit never landed), and
a bucket whose warehouse record was deleted WITHOUT `?purge_bucket=true` — which the delete door
creates deliberately, because a catalog entry is recoverable and a customer's bucket is not.

**Referenced means referenced by ANY LIVE VERSION, not by the latest one.** Every manifest still in
`_versions/` is reachable via time-travel until `cleanup_old_versions` removes it, so a file only the
previous version cites is LIVE, not garbage. Computing the referenced set from the current version
alone would report most of a healthy dataset's history as orphaned — a report that would destroy the
table if anyone acted on it.
"""

from __future__ import annotations

import logging
import posixpath

import lance
import pyarrow.fs as pafs
from pydantic import BaseModel, Field

from maintenance.core.config import shared_lance_session
from service_kit.lakehouse.features import unsupported_features, unsupported_features_from_open_error


log = logging.getLogger(__name__)


class _OverlaysPresent(Exception):
    """A fragment carries data overlay files (feature flag 64), which this pass does not understand."""

    def __init__(self, dataset_uri: str) -> None:
        super().__init__(
            f"{dataset_uri}: uses data overlay files (feature flag 64). The spec requires a reader that "
            "does not understand overlays to REFUSE the dataset — an overlay lives in `data/` and is "
            "referenced from DataFragment.overlays, not data_files(), so scanning would report live "
            "cell values as reclaimable."
        )


#: Directories Lance owns inside a dataset. Anything here is structural, not user data.
_DATA_DIR = "data"
_DELETIONS_DIR = "_deletions"
_INDICES_DIR = "_indices"
_TRANSACTIONS_DIR = "_transactions"
_VERSIONS_DIR = "_versions"
#: Tags and branches — NAMED VERSION POINTERS, and live metadata by definition. A tag PINS a version
#: (and `cleanup_old_versions` exempts tagged versions for exactly that reason), so a tag file can
#: never be unreferenced while it exists. Found the hard way: the first live run reported every
#: `publish-*` promotion tag in the estate as an orphan — the precise failure this module exists to
#: avoid, since a reclaimer acting on it would unpin published data.
_REFS_DIR = "_refs"

#: Named branches live here — `tree/{branch_name}/` holds a WHOLE parallel dataset (its own
#: `_versions/`, `_transactions/`, `_deletions/`, `_indices/`). Branch names may contain `/`, so this
#: is a path prefix, not one segment.
_TREE_DIR = "tree"

#: MemWAL shards — an LSM tree beside the base table. Each `_mem_wal/{shard}/` holds `manifest/`,
#: `wal/`, and `{random8}_gen_{i}/` SSTable directories, every SSTable itself a full Lance dataset.
#: None of it is reachable from the base table's manifest.
_MEM_WAL_DIR = "_mem_wal"

#: A zero-byte marker Lance writes at the dataset root. Structural, never referenced by a manifest,
#: so a naive scan reports it once per dataset forever.
_RESERVED_MARKER = ".lance-reserved"

#: Ceiling on versions inspected per dataset. A table with a very long history would otherwise make
#: one report O(versions x fragments) — and this pass runs over every dataset in every bucket.
#: Hitting it is REPORTED (`incomplete`), never silently truncated: a partial referenced-set would
#: mark live files as orphans, the single most dangerous way this pass can be wrong.
_MAX_VERSIONS = 500


class OrphanFile(BaseModel):
    """One file under a dataset prefix that no live version's manifest references."""

    #: The dataset it was found under (a URI), so a finding is actionable without re-deriving it.
    dataset: str
    #: Path relative to the dataset prefix — `data/x.lance`, `_transactions/3-uuid.txn`.
    path: str
    #: Which Lance-owned area it sits in: data / deletions / indices / transactions / versions / other.
    kind: str
    size_bytes: int = 0


class DatasetOrphanScan(BaseModel):
    """One dataset's result. `checked` false means the dataset could not be read — and then `orphans`
    is EMPTY BY CONSTRUCTION rather than "clean", which the report must not conflate."""

    dataset: str
    checked: bool
    orphans: list[OrphanFile] = Field(default_factory=list)
    versions_scanned: int = 0
    reason: str | None = None


class OrphanReport(BaseModel):
    """Unreferenced files across every scanned dataset. Machine-readable so a later reclaimer can
    consume exactly what a human already reviewed."""

    datasets_scanned: int = 0
    datasets_unreadable: int = 0
    orphans: list[OrphanFile] = Field(default_factory=list)
    #: Sources that answered PARTIALLY — a version ceiling, an unlistable prefix. Named so the reader
    #: knows which findings to distrust rather than assuming completeness.
    incomplete: list[str] = Field(default_factory=list)
    total: int = 0


def _kind_of(rel_path: str) -> str:
    head = rel_path.split("/", 1)[0]
    return {
        _DATA_DIR: "data",
        _DELETIONS_DIR: "deletions",
        _INDICES_DIR: "indices",
        _TRANSACTIONS_DIR: "transactions",
        _VERSIONS_DIR: "versions",
        _REFS_DIR: "refs",
    }.get(head, "other")


def open_dataset(dataset_uri: str, storage_options: dict[str, str] | None = None) -> lance.LanceDataset:
    """The scan's ONE open per dataset — threading the shared bounded session (#102).

    Split out of :func:`referenced_paths` so :func:`scan_dataset` can hold the handle and hand it to
    the layout gate (which reads the manifest's feature flags) without a second open.
    """
    return lance.dataset(dataset_uri, storage_options=storage_options, session=shared_lance_session())


def referenced_paths(dataset_uri: str, storage_options: dict[str, str] | None = None) -> tuple[set[str], int, str | None]:
    """Every dataset-relative path referenced by ANY live version. Returns ``(paths, versions, note)``.

    Read-only: opens each version and reads its fragment metadata. The union across versions is the
    point — see the module docstring on why the latest version alone is not the referenced set.

    Sidecar DIRECTORIES are returned as trailing-slash entries, so the caller can prefix-test them
    without a second return value.

    ``note`` is non-None when the version ceiling was hit, in which case the caller must treat the
    referenced set as INCOMPLETE and report nothing as an orphan from it.
    """
    return referenced_paths_of(open_dataset(dataset_uri, storage_options), dataset_uri)


def referenced_paths_of(ds: lance.LanceDataset, dataset_uri: str) -> tuple[set[str], int, str | None]:
    """:func:`referenced_paths` over an ALREADY-OPEN dataset. Same contract, no open."""
    referenced: set[str] = set()
    referenced_dirs: set[str] = set()
    versions = [v["version"] for v in ds.versions()]
    note = None
    if len(versions) > _MAX_VERSIONS:
        note = f"{dataset_uri}: {len(versions)} versions exceeds the {_MAX_VERSIONS} ceiling — referenced set is INCOMPLETE"
        versions = versions[-_MAX_VERSIONS:]

    for version in versions:
        # checkout_version, NOT a fresh lance.dataset(): the constructor mints a new cache pair per
        # call — up to _MAX_VERSIONS times per dataset, the scan's real N+1 (#102) — while checkout
        # reuses the open dataset's session (its documented contract, verified empirically).
        at = ds.checkout_version(version)
        for fragment in at.get_fragments():
            for data_file in fragment.data_files():
                referenced.add(f"{_DATA_DIR}/{data_file.path}")
                # BLOB SIDECARS. A large-binary column does not live inside the `.lance` file; its
                # bytes sit in `data/<data-file-stem>/*.blob` beside it. `data_files()` names only the
                # `.lance`, so a scan that stops there reports every blob in the estate as garbage —
                # measured live 2026-08-04 on the bronze page-image table: 29 MB of real page images,
                # named as reclaimable. Mark the sidecar DIRECTORY of every referenced data file as
                # referenced too.
                #
                # Sidecars whose parent data file is referenced by NO live version are still reported,
                # and legitimately so: Lance's own `cleanup_old_versions` reclaims the `.lance` and
                # leaves the sidecar, which is precisely the reclamation gap this pass exists to name.
                referenced_dirs.add(f"{_DATA_DIR}/{data_file.path.removesuffix('.lance')}")
            # OVERLAY FILES (feature flag 64). An overlay writes new values for a subset of cells to
            # `data/overlay-<uuid>.lance` — inside `data/`, where a false positive deletes real
            # values — and is referenced from `DataFragment.overlays`, not `data_files()`. The spec
            # settles it: "a reader or writer that does not understand overlay files must REFUSE a
            # dataset that uses them", because ignoring one returns stale base values, "a correctness
            # bug rather than a degraded experience". Refusing is the conforming answer.
            #
            # Overlays ARE writable on pylance 9.0.0 (`LanceOperation.DataOverlay`, verified by
            # committing one), but pylance then REFUSES to open the result — so on today's pylance
            # this branch is unreachable and the refusal arrives via the manifest feature-flag gate
            # instead (`_unscannable_reason`, `service_kit.lakehouse.features`). Kept as the second line
            # of defence for the pylance that gains overlay READ support: that build opens the
            # dataset, and this is the seam the walk would otherwise scan straight past.
            if getattr(fragment.metadata, "overlays", None):
                raise _OverlaysPresent(dataset_uri)
            deletion = fragment.metadata.deletion_file
            if deletion is not None:
                # `path()` renders the on-disk name from (fragment id, read version, unique id); the
                # naming is Lance's, so asking it beats reconstructing the convention here.
                referenced.add(deletion.path(fragment.fragment_id))
    # THE TRANSACTION FILE OF EVERY LIVE COMMIT IS REFERENCED, and omitting it reported the whole
    # class as garbage. Nothing here ever added a `_transactions/*.txn` path, so a dataset with N live
    # versions had all N of its transaction files named as orphans — including the one that produced
    # the CURRENT version.
    #
    # The module docstring above is right that txn files from FAILED or rolled-back commits accumulate
    # by design and nothing prunes them. It does not follow that every txn is garbage: the ones
    # belonging to live versions are the provenance of the manifests being read. MEASURED live
    # 2026-08-16 — `s3://lance-catalog/bronze/pages` has exactly ONE live version and exactly ONE txn
    # file, and the scan reported that file as an orphan. Estate-wide, 34 of 56 `orphan_files` were
    # this, which is why the count could never reach zero and the #79 purge gate could never certify.
    #
    # The name is `{read_version}-{uuid}.txn`, both fields exposed on the `Transaction`. Asked for
    # `len(versions)` of them rather than the default 10, so the referenced set covers the same
    # versions the loop above walked; a failure to read them is NOT fatal but DOES void the set,
    # because a partial referenced set is exactly what makes a reclaimer delete live data.
    try:
        for transaction in ds.get_transactions(recent_transactions=max(len(versions), 1)):
            if transaction is None:
                continue
            referenced.add(f"{_TRANSACTIONS_DIR}/{transaction.read_version}-{transaction.uuid}.txn")
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 — any failure here must void the set, never narrow it
        # BaseException, not Exception, and that is not defensive over-reach. pylance 10.0.0 PANICS in
        # Rust on `get_transactions()` against a SHALLOW CLONE — `src/transaction.rs:735: not yet
        # implemented` — and `pyo3_runtime.PanicException` derives from BaseException directly, so
        # `except Exception` does not see it. Measured 2026-08-16 while upgrading pylance 9 -> 10.
        #
        # The consequence was the exact inversion of this function's contract: instead of voiding the
        # referenced set for ONE dataset and carrying on, a single shallow clone anywhere in the estate
        # took the WHOLE orphan scan down — and the scan walks every warehouse bucket. Interrupts are
        # re-raised above so this cannot swallow a Ctrl-C or a shutdown.
        note = note or f"{dataset_uri}: transaction files unreadable ({type(exc).__name__}) — referenced set is INCOMPLETE"

    # Sidecar directories are expanded into the path set by the caller's prefix test, kept separate
    # here so the exact-match set stays exact.
    referenced |= {f"{d}/" for d in referenced_dirs}
    return referenced, len(versions), note


def _unscannable_reason(ds: lance.LanceDataset, *, fs: pafs.FileSystem, prefix: str, referenced: set[str]) -> str | None:
    """Why this dataset CANNOT be scanned safely, or ``None`` if it can.

    Layouts that make the "list the prefix, subtract the referenced set" method produce false
    positives on LIVE data. All are refused rather than approximated, because the failure mode is a
    reclaimer deleting a branch or another dataset's files.

    **Unsupported manifest feature flags (#64) — checked FIRST.** The flags are the format's own
    answer to "is this a layout you understand", and the spec requires a reader that does not know a
    flag to refuse the dataset outright. Reading them (`service_kit.lakehouse.features`) catches the whole
    class at once, including shapes with no observable consequence yet: `add_bases` registers a base
    path that no DataFile resolves through, so the consequence check below sees nothing wrong and
    this scan previously returned `checked=True` with orphans named on a multi-base dataset
    (measured). The two checks are complementary, not redundant — see the base_paths note below.

    **Branches.** A branch is a shallow clone of its parent whose `_versions/`, `_transactions/`,
    `_deletions/` and `_indices/` live under `tree/{branch}/`. `lance.dataset(uri)` opens the MAIN
    branch, so nothing under `tree/` is ever in the referenced set — every file of every branch is
    unreferenced BY CONSTRUCTION. Measured: a two-commit branch made 6 of 7 findings branch files,
    including the branch's own `data/*.lance`. Branching sets NO feature flag, so the directory probe
    is the only thing that sees it.

    **Multi-base / shallow clones.** A manifest's `base_paths[]` lets a DataFile, DeletionFile or
    index resolve under ANOTHER dataset root (feature flag 16). The flag check above names it
    directly; this consequence check — a referenced path that is not present under this prefix —
    STAYS, because it is the broader of the two: it also catches a dataset whose files do not all
    live here for a reason no flag records. The mirror hazard is worse and invisible from here — the
    SOURCE of a clone holds files that only the CLONE's manifest still references, so scanning the
    source alone would name them garbage, and the source carries no flag at all.
    """
    if (refusal := unsupported_features(ds)) is not None:
        return f"{prefix}: {refusal}"

    for directory, why in (
        (
            _TREE_DIR,
            "has branches (`tree/`), whose files are unreferenced by the main branch BY CONSTRUCTION — scanning would report every branch as garbage",
        ),
        (
            _MEM_WAL_DIR,
            "has MemWAL shards (`_mem_wal/`) — WAL entries and SSTable datasets that no base-table "
            "manifest references. Reclaiming there needs the shard manifests, and the spec warns that "
            "deleting WAL files WEAKENS writer fencing (fencing detects a stalled writer by a "
            "put-if-not-exists COLLISION, which GC removes)",
        ),
    ):
        try:
            probe = fs.get_file_info(f"{prefix}/{directory}")
        except Exception as exc:
            # A merely-absent probe returns NotFound; a RAISE is a transient object-store / permission
            # failure — we could not determine whether this layout is present, and a layout we cannot
            # rule out is one we must not certify. Fail CLOSED so the dataset reads checked=False rather
            # than falling through to a scan that would name a live branch's files as orphans.
            return f"{prefix}: layout probe for `{directory}/` failed ({type(exc).__name__}: {exc}) — cannot certify the dataset is scannable"
        if probe.type == pafs.FileType.Directory:
            return f"{prefix}: {why}"

    # A referenced path that is not here means the dataset spans roots (base_paths / shallow clone).
    # Checked against the exact-match entries only; the trailing-slash entries are sidecar DIRECTORIES.
    #
    # ONE BATCHED CALL, not one round trip per referenced path. This was a comprehension issuing
    # `get_file_info(path)` per entry, sequentially — and the referenced set holds one entry per data
    # file, deletion file, index and transaction across every live version, so a dataset with history
    # paid a burst of serial HEADs, on every dataset in every warehouse bucket, on every reconcile
    # tick, before a single orphan had been found. `get_file_info` takes a LIST and the filesystem
    # answers it as a batch (pyarrow's S3 implementation fans the batch out over its IO pool rather
    # than issuing them one behind the other). It also removes the question of short-circuiting: one
    # call cannot be cut short, and the full answer is what the message below reports.
    #
    # Guarded on the SAME rule as the two directory probes above, which it did not share: a raise here
    # left the whole orphan scan (and, through `reconcile`, the whole tick) — one dataset's transient
    # HeadObject failure taking down a report about the entire estate. Fail CLOSED for the same reason:
    # a layout we could not determine is one we must not certify as scannable.
    exact = [rel for rel in sorted(referenced) if not rel.endswith("/")]
    try:
        present = fs.get_file_info([f"{prefix}/{rel}" for rel in exact])
    except Exception as exc:
        return f"{prefix}: base-path presence probe failed ({type(exc).__name__}: {exc}) — cannot certify the dataset is scannable"
    missing = [rel for rel, info in zip(exact, present, strict=True) if info.type != pafs.FileType.File]
    if missing:
        return (
            f"{prefix}: {len(missing)} referenced file(s) do not live under this prefix (e.g. {missing[0]}) — "
            "the dataset spans base_paths (shallow clone / multi-base), so a prefix listing cannot be subtracted"
        )
    return None


def scan_dataset(fs: pafs.FileSystem, dataset_uri: str, *, prefix: str, storage_options: dict[str, str] | None = None) -> DatasetOrphanScan:
    """List one dataset's files and subtract what any live version references.

    ``prefix`` is the dataset's path as the filesystem sees it (no scheme), because pyarrow lists by
    path while Lance opens by URI — and it is KEYWORD-ONLY because it is a second `str` beside
    ``dataset_uri`` that very often carries the SAME value, which is precisely the pair a call site
    cannot self-check (MAINT-16).

    A dataset that cannot be READ yields ``checked=False`` and NO orphans. That distinction is the
    whole safety property: "we could not determine the referenced set" must never render as "none of
    these files are referenced", which is the shape that would delete a live table.
    """
    try:
        ds = open_dataset(dataset_uri, storage_options)
        referenced, versions, note = referenced_paths_of(ds, dataset_uri)
    except Exception as exc:
        # pylance refuses a manifest whose feature flags IT does not know (measured: a committed data
        # overlay, flag 64) — a REFUSAL, and it must read as one. Reported raw it is a Rust source
        # path from a GitHub CI runner, which tells the report's reader nothing about why. An
        # ordinary unopenable path keeps its existing `TypeName: message` shape.
        refusal = unsupported_features_from_open_error(exc)
        reason = f"{prefix}: {refusal}" if refusal is not None else f"{type(exc).__name__}: {exc}"
        log.warning("orphan_scan_unreadable", extra={"dataset": dataset_uri, "error": str(exc)})
        return DatasetOrphanScan(dataset=dataset_uri, checked=False, reason=reason)

    # LAYOUT GATE FIRST, then the incomplete-set note. Both refuse, so the order changes only which
    # REASON is reported — and the layout answer is the better one: "this dataset spans base_paths" is a
    # structural fact about the dataset, while "transaction files unreadable" is a fact about one read
    # that failed. Ordering mattered from pylance 10.0.0, which panics on `get_transactions()` for a
    # shallow clone: the note then fired first and shadowed the base_paths refusal, so the operator was
    # told the least useful of the two true things.
    if (unscannable := _unscannable_reason(ds, fs=fs, prefix=prefix, referenced=referenced)) is not None:
        log.warning("orphan_scan_skipped", extra={"dataset": dataset_uri, "reason": unscannable})
        return DatasetOrphanScan(dataset=dataset_uri, checked=False, versions_scanned=versions, reason=unscannable)

    if note:
        return DatasetOrphanScan(dataset=dataset_uri, checked=False, versions_scanned=versions, reason=note)

    orphans: list[OrphanFile] = []
    try:
        entries = fs.get_file_info(pafs.FileSelector(prefix, allow_not_found=True, recursive=True))
    except Exception as exc:
        log.warning("orphan_listing_failed", extra={"dataset": dataset_uri, "error": str(exc)})
        return DatasetOrphanScan(dataset=dataset_uri, checked=False, versions_scanned=versions, reason=f"listing failed: {exc}")

    # Materialized ONCE, outside the loop. The sidecar test used to re-filter the whole referenced set
    # for every listed file that was not an exact match — O(files x referenced) Python-level work per
    # dataset, on the same hot path as the probe batching above (the audit's HOUSE-RULE-16 addendum).
    sidecar_dirs = tuple(d for d in referenced if d.endswith("/"))
    for info in entries:
        if info.type != pafs.FileType.File:
            continue
        rel = posixpath.relpath(info.path, prefix)
        if rel in referenced:
            continue
        # ...or it sits under a referenced data file's blob-sidecar directory.
        if rel.startswith(sidecar_dirs):
            continue
        # Manifests and the hint are the VERSION INDEX itself, not payload: a manifest is what makes a
        # version live, so it can never be "unreferenced" while it is present, and the hint is
        # explicitly disposable. Reporting either every run would bury the findings that matter.
        if rel.startswith(f"{_VERSIONS_DIR}/") or rel.startswith(f"{_REFS_DIR}/") or rel == _RESERVED_MARKER:
            continue
        orphans.append(OrphanFile(dataset=dataset_uri, path=rel, kind=_kind_of(rel), size_bytes=info.size or 0))

    return DatasetOrphanScan(dataset=dataset_uri, checked=True, orphans=orphans, versions_scanned=versions)


def scan_datasets(fs: pafs.FileSystem, datasets: list[tuple[str, str]], storage_options: dict[str, str] | None = None) -> OrphanReport:
    """Run :func:`scan_dataset` over ``(dataset_uri, prefix)`` pairs and aggregate.

    An unreadable dataset increments ``datasets_unreadable`` and lands in ``incomplete`` — it does NOT
    silently reduce the orphan count, because "we could not look" and "there was nothing there" are
    different answers and only one of them is safe to act on.
    """
    report = OrphanReport()
    for dataset_uri, prefix in datasets:
        result = scan_dataset(fs, dataset_uri, prefix=prefix, storage_options=storage_options)
        if not result.checked:
            report.datasets_unreadable += 1
            report.incomplete.append(result.reason or f"{dataset_uri}: unreadable")
            continue
        report.datasets_scanned += 1
        report.orphans.extend(result.orphans)
    report.total = len(report.orphans)
    return report
