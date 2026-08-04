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


log = logging.getLogger(__name__)

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

#: A zero-byte marker Lance writes at the dataset root. Structural, never referenced by a manifest,
#: so a naive scan reports it once per dataset forever.
_RESERVED_MARKER = ".lance-reserved"

#: Written by Lance as a discovery shortcut. The spec: "purely an optimization", "always safe to
#: delete". It is never referenced by a manifest, so a naive scan would report it every single run —
#: which is how a report trains its reader to ignore it. Classified, not listed as drift.
_VERSION_HINT = "latest_version_hint.json"

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


def referenced_paths(dataset_uri: str, storage_options: dict[str, str] | None = None) -> tuple[set[str], int, str | None]:
    """Every dataset-relative path referenced by ANY live version. Returns ``(paths, versions, note)``.

    Read-only: opens each version and reads its fragment metadata. The union across versions is the
    point — see the module docstring on why the latest version alone is not the referenced set.

    Sidecar DIRECTORIES are returned as trailing-slash entries, so the caller can prefix-test them
    without a second return value.

    ``note`` is non-None when the version ceiling was hit, in which case the caller must treat the
    referenced set as INCOMPLETE and report nothing as an orphan from it.
    """
    referenced: set[str] = set()
    referenced_dirs: set[str] = set()
    ds = lance.dataset(dataset_uri, storage_options=storage_options)
    versions = [v["version"] for v in ds.versions()]
    note = None
    if len(versions) > _MAX_VERSIONS:
        note = f"{dataset_uri}: {len(versions)} versions exceeds the {_MAX_VERSIONS} ceiling — referenced set is INCOMPLETE"
        versions = versions[-_MAX_VERSIONS:]

    for version in versions:
        at = lance.dataset(dataset_uri, version=version, storage_options=storage_options)
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
            deletion = fragment.metadata.deletion_file
            if deletion is not None:
                # `path()` renders the on-disk name from (fragment id, read version, unique id); the
                # naming is Lance's, so asking it beats reconstructing the convention here.
                referenced.add(deletion.path(fragment.fragment_id))
    # Sidecar directories are expanded into the path set by the caller's prefix test, kept separate
    # here so the exact-match set stays exact.
    referenced |= {f"{d}/" for d in referenced_dirs}
    return referenced, len(versions), note


def _unscannable_reason(fs: pafs.FileSystem, prefix: str, referenced: set[str]) -> str | None:
    """Why this dataset CANNOT be scanned safely, or ``None`` if it can.

    Two layouts make the "list the prefix, subtract the referenced set" method produce false
    positives on LIVE data. Both are refused rather than approximated, because the failure mode is a
    reclaimer deleting a branch or another dataset's files.

    **Branches.** A branch is a shallow clone of its parent whose `_versions/`, `_transactions/`,
    `_deletions/` and `_indices/` live under `tree/{branch}/`. `lance.dataset(uri)` opens the MAIN
    branch, so nothing under `tree/` is ever in the referenced set — every file of every branch is
    unreferenced BY CONSTRUCTION. Measured: a two-commit branch made 6 of 7 findings branch files,
    including the branch's own `data/*.lance`.

    **Multi-base / shallow clones.** A manifest's `base_paths[]` lets a DataFile, DeletionFile or
    index resolve under ANOTHER dataset root (feature flag 16). pylance does not expose `base_paths`,
    so this is detected by its consequence instead, which is strictly more robust: if a path the
    manifest REFERENCES is not present under this prefix, the dataset's files do not all live here.
    A prefix listing therefore cannot be subtracted from a referenced set that spans roots. The
    mirror hazard is worse and invisible from here — the SOURCE of a clone holds files that only the
    CLONE's manifest still references, so scanning the source alone would name them garbage.
    """
    tree = f"{prefix}/{_TREE_DIR}"
    try:
        if fs.get_file_info(tree).type == pafs.FileType.Directory:
            return (
                f"{prefix}: has branches (`{_TREE_DIR}/`), whose files are unreferenced by the main branch "
                "BY CONSTRUCTION — scanning would report every branch as garbage"
            )
    except Exception:  # noqa: S110 — an unstattable path is not evidence of branches
        pass

    # A referenced path that is not here means the dataset spans roots (base_paths / shallow clone).
    # Checked against the exact-match entries only; the trailing-slash entries are sidecar DIRECTORIES.
    missing = [rel for rel in sorted(referenced) if not rel.endswith("/") and fs.get_file_info(f"{prefix}/{rel}").type != pafs.FileType.File]
    if missing:
        return (
            f"{prefix}: {len(missing)} referenced file(s) do not live under this prefix (e.g. {missing[0]}) — "
            "the dataset spans base_paths (shallow clone / multi-base), so a prefix listing cannot be subtracted"
        )
    return None


def scan_dataset(fs: pafs.FileSystem, dataset_uri: str, prefix: str, storage_options: dict[str, str] | None = None) -> DatasetOrphanScan:
    """List one dataset's files and subtract what any live version references.

    ``prefix`` is the dataset's path as the filesystem sees it (no scheme), because pyarrow lists by
    path while Lance opens by URI.

    A dataset that cannot be READ yields ``checked=False`` and NO orphans. That distinction is the
    whole safety property: "we could not determine the referenced set" must never render as "none of
    these files are referenced", which is the shape that would delete a live table.
    """
    try:
        referenced, versions, note = referenced_paths(dataset_uri, storage_options)
    except Exception as exc:
        log.warning("orphan_scan_unreadable", extra={"dataset": dataset_uri, "error": str(exc)})
        return DatasetOrphanScan(dataset=dataset_uri, checked=False, reason=f"{type(exc).__name__}: {exc}")

    if note:
        return DatasetOrphanScan(dataset=dataset_uri, checked=False, versions_scanned=versions, reason=note)

    # Layout gate — refuse the two shapes whose false positives are LIVE data (see _unscannable_reason).
    if (unscannable := _unscannable_reason(fs, prefix, referenced)) is not None:
        log.warning("orphan_scan_skipped", extra={"dataset": dataset_uri, "reason": unscannable})
        return DatasetOrphanScan(dataset=dataset_uri, checked=False, versions_scanned=versions, reason=unscannable)

    orphans: list[OrphanFile] = []
    try:
        entries = fs.get_file_info(pafs.FileSelector(prefix, allow_not_found=True, recursive=True))
    except Exception as exc:
        log.warning("orphan_listing_failed", extra={"dataset": dataset_uri, "error": str(exc)})
        return DatasetOrphanScan(dataset=dataset_uri, checked=False, versions_scanned=versions, reason=f"listing failed: {exc}")

    for info in entries:
        if info.type != pafs.FileType.File:
            continue
        rel = posixpath.relpath(info.path, prefix)
        if rel in referenced:
            continue
        # ...or it sits under a referenced data file's blob-sidecar directory.
        if any(rel.startswith(d) for d in referenced if d.endswith("/")):
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
        result = scan_dataset(fs, dataset_uri, prefix, storage_options)
        if not result.checked:
            report.datasets_unreadable += 1
            report.incomplete.append(result.reason or f"{dataset_uri}: unreadable")
            continue
        report.datasets_scanned += 1
        report.orphans.extend(result.orphans)
    report.total = len(report.orphans)
    return report
