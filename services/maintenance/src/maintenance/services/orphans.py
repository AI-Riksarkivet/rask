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
* `_transactions/*.txn` files from failed or rolled-back commits — a conflict leaves them behind
  ("transaction files remain in storage describing each commit attempt"), so a busy table accrues
  them. Whether Lance will ever take one depends on its DIRECTORY. `cleanup_old_versions` clamps its
  listing of `_transactions/` and `_deletions/` to the commit timestamp of the earliest RETAINED
  manifest before the 7-day unverified rule filters what was listed, while `data/` is listed through
  `max(floor, now - 7 days)` (all of it with `delete_unverified=True`, lance #8944) and `_indices/` in
  full (lance v13.0.0 `rust/lance/src/dataset/cleanup.rs:756-778`). Measured against pylance 11.0.0
  with `delete_unverified=True` bypassing the age filter, a transaction file flips at that timestamp to
  the second: offsets -3600/-60/-1/0 s are removed, +1/+60/+3600 s are kept; on pylance 13.0.0 a data
  file above the floor is removed. So a transaction or deletion file written AFTER the last surviving
  commit is invisible to cleanup at any `older_than` and with any flag — permanently, once a dataset
  has collapsed to one live version — while a data or index file, and any file below the floor, clears
  itself once it passes 7 days. `OrphanFile.reclaimable_by_lance` carries which, because only the
  first class justifies a reclaimer;
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
import time
from collections.abc import Iterator

import lance
import pyarrow.fs as pafs
from pydantic import BaseModel, Field

from lakehouse.features import unsupported_features, unsupported_features_from_open_error
from lakehouse.lancekit.versions import committed_at
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import shared_lance_session


log = logging.getLogger(__name__)


class _FilesPastTheCeiling(Exception):
    """A dataset's live versions reference more files than one scan probes (:data:`_MAX_REFERENCED_FILES`)."""

    def __init__(self, dataset_uri: str, files: int) -> None:
        super().__init__(
            f"{dataset_uri}: {files} referenced files exceed the {_MAX_REFERENCED_FILES}-file ceiling, so its unreferenced files were not looked for"
        )


class _ListingPastTheCeiling(Exception):
    """More files lie under a dataset than one scan lists (:data:`_MAX_LISTED_FILES`)."""

    def __init__(self, dataset_uri: str) -> None:
        super().__init__(f"{dataset_uri}: more than {_MAX_LISTED_FILES} files lie under it, past the listing ceiling, so its unreferenced files were not named")


class _HistoryPastTheCeiling(Exception):
    """A dataset keeps more live versions than one scan walks (:data:`_MAX_VERSIONS`)."""

    def __init__(self, dataset_uri: str, versions: int) -> None:
        super().__init__(f"{dataset_uri}: {versions} versions exceed the {_MAX_VERSIONS}-version ceiling, so its unreferenced files were not looked for")


class _ReferencedElsewhere(Exception):
    """A file a retained version references is not listed under the dataset's root, so the root's listing is not the
    dataset's files and subtracting the referenced set from it names nothing reliably."""

    def __init__(self, dataset_uri: str, missing: set[str]) -> None:
        super().__init__(
            f"{dataset_uri}: {len(missing)} referenced file(s) are not listed under it (e.g. {min(missing)}) — a file of the"
            " dataset lives under another root (shallow clone / multi-base) or is gone, so a listing of its root cannot be subtracted"
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

#: The ``type`` ``tracked_files()`` gives a data file (pylance 13.0.0), whose blob sidecar directory it does not name.
_TRACKED_DATA_FILE = "data file"

#: Ceiling on versions inspected per dataset. A table with a very long history would otherwise make
#: one scan O(versions x fragments). A dataset past it is EXCLUDED before any version is read; it is
#: never walked in part, because a partial referenced set would mark live files as orphans, the single
#: most dangerous way this pass can be wrong. The tables it reaches are the busy ones, whose expiry keeps
#: raising the listing floor that floor-bound residue waits behind.
_MAX_VERSIONS = 500

#: Ceiling on the files one scan's referenced set holds: the set is kept whole while the listing is matched against it,
#: so a table referencing more is EXCLUDED, like one past :data:`_MAX_VERSIONS`, before its listing, and a huge table
#: does not hold a replica past its bound.
_MAX_REFERENCED_FILES = 50_000

#: Ceiling on the files one scan lists under a dataset, read a page at a time and never held whole: a table past it is
#: EXCLUDED. Without it a writer that crash-loops, leaving files and committing nothing, grows a listing no ceiling on
#: the REFERENCED files sees, and the findings it becomes (measured 2026-11-10: 1.4 KiB a file held, 239 MiB for
#: 200,000) end the worker at its memory limit on every delivery.
_MAX_LISTED_FILES = 100_000

#: The kinds `cleanup_old_versions` lists PAST the listing floor (lance v13.0.0 `cleanup.rs:764-777`):
#: `data/` through `max(floor, now - 7 days)` since lance #8944, `_indices/` with no cutoff. An
#: unreferenced file of these kinds is cleared by time alone, so it is never stranded. Every other kind
#: is judged against the floor.
_LISTED_PAST_THE_FLOOR_KINDS = frozenset({"data", "indices"})


class OrphanFile(BaseModel):
    """One file under a dataset prefix that no live version's manifest references."""

    #: The dataset it was found under (a URI), so a finding is actionable without re-deriving it.
    dataset: str
    #: Path relative to the dataset prefix — `data/x.lance`, `_transactions/3-uuid.txn`.
    path: str
    #: Which Lance-owned area it sits in: data / deletions / indices / transactions / versions / other.
    kind: str
    size_bytes: int = 0
    #: True when `cleanup_old_versions` can still SEE this file, so time alone clears it (always, for a
    #: data or index file); False when it is a floor-bound kind above the listing floor and no Lance call
    #: will ever enumerate it; None when the floor could not be read.
    #:
    #: The distinction is what makes a finding actionable. Measured on the deployed estate 2026-09-18,
    #: the orphan count fell 1011 -> 32 in ten minutes as the ordinary sweep took 979 files at the
    #: +7-day anniversary of the writes that made them, so only the residue is a real finding.
    reclaimable_by_lance: bool | None = None
    #: The file's own mtime as an epoch, which is what the floor is compared against. Kept on the
    #: finding so the classification is reproducible from the report alone, without re-listing.
    mtime_epoch: float = 0.0


class DatasetOrphanScan(BaseModel):
    """One dataset's result. `checked` false means the dataset could not be read — and then `orphans`
    is EMPTY BY CONSTRUCTION rather than "clean", which the report must not conflate."""

    dataset: str
    checked: bool
    orphans: list[OrphanFile] = Field(default_factory=list)
    versions_scanned: int = 0
    reason: str | None = None
    #: TRUE when the scan does not examine this dataset BY RULE, as opposed to a scan that tried and
    #: failed. Only the second is a partial answer, which a later try may complete. Two kinds are excluded:
    #:
    #: * a layout the orphan method does not APPLY to. The spec is what makes these permanent rather
    #:   than merely sticky. `lance_docs/lance/format/table/versioning.md` § Feature Flags: a reader seeing a flag it does not know
    #:   "should return an 'unsupported' error on any read or write operation", and flag 16
    #:   `FLAG_BASE_PATHS` (shallow clones / multi-base) is Reader Required. The flag lives in the
    #:   MANIFEST, so the refusal clears when this code learns base_paths and never on a later tick.
    #:   Measured 2026-09-08: 419 of the estate's 490 incomplete notes were that refusal, and
    #:   `incomplete` held at exactly 490 across a day in which the finding total moved.
    #: * a history past :data:`_MAX_VERSIONS`, more referenced files than :data:`_MAX_REFERENCED_FILES`, or more
    #:   listed files than :data:`_MAX_LISTED_FILES`, which bound cost.
    excluded: bool = False


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


def open_dataset(dataset_uri: str, storage_options: StorageOptions | None = None) -> lance.LanceDataset:
    """The scan's ONE open per dataset — threading the shared bounded session (#102).

    :func:`scan_dataset` holds the handle and hands it to :func:`referenced_paths_of` and to the layout gate
    (which reads the manifest's feature flags), so neither opens the dataset again.
    """
    return lance.dataset(dataset_uri, storage_options=None if storage_options is None else storage_options.in_clear(), session=shared_lance_session())


def referenced_paths_of(ds: lance.LanceDataset, dataset_uri: str) -> tuple[set[str], int]:
    """Every dataset-relative path a retained version references, and how many versions there are.

    Read off Lance's own ``tracked_files()``, which names the manifest, data, deletion, transaction and index files of
    every manifest in ``_versions/``, so a file only an old version cites is referenced (the module docstring says why
    the latest version alone is not the referenced set). Measured on pylance 13.0.0: a tagged version kept through a
    cleanup of the versions after it, its transaction file included, every index type's files (BTREE, BITMAP,
    LABEL_LIST, INVERTED, IVF_PQ, and a segment a replace left to an older version), and every file under a referenced
    index segment's directory, one planted there included. A file named through another base is returned by its path
    under that base, which the layout gate then does not find under this prefix.

    It names no blob sidecar. A large-binary column's bytes lie in ``data/<data file stem>/*.blob`` beside the data file
    (``lance_docs/lance/format/table/blob.md`` § Sidecar Resolution), so each tracked data file's sidecar directory is
    returned as a trailing-slash entry the caller matches by prefix: measured live 2026-08-04 on a page-image table, a
    scan that stopped at the ``.lance`` named 29 MB of live page images reclaimable. A sidecar whose data file no
    retained version references is still reported, and ``reclaimable_by_lance`` says Lance's cleanup takes it.

    The count comes from ``version_refs()``, which reads no manifest (measured on a 120-version dataset: 0.5 ms against
    ``versions()``'s 9.3 ms), and a dataset past :data:`_MAX_VERSIONS` is excluded before any manifest is read.

    Raises:
        _HistoryPastTheCeiling: the dataset keeps more than :data:`_MAX_VERSIONS` versions; none was read.
    """
    versions = len(ds.version_refs())
    if versions > _MAX_VERSIONS:
        raise _HistoryPastTheCeiling(dataset_uri, versions)
    referenced: set[str] = set()
    for batch in ds.tracked_files():
        for path, kind in zip(batch.column("path").to_pylist(), batch.column("type").to_pylist(), strict=True):
            referenced.add(path)
            if kind == _TRACKED_DATA_FILE:
                referenced.add(f"{path.removesuffix('.lance')}/")
    return referenced, versions


def _unscannable_reason(ds: lance.LanceDataset, *, fs: pafs.FileSystem, prefix: str) -> str | None:
    """Why this dataset CANNOT be scanned safely, or ``None`` if it can.

    Layouts that make the "list the prefix, subtract the referenced set" method produce false
    positives on LIVE data. All are refused rather than approximated, because the failure mode is a
    reclaimer deleting a branch or another dataset's files.

    **Unsupported manifest feature flags (#64) — checked FIRST.** The flags are the format's own
    answer to "is this a layout you understand", and the spec requires a reader that does not know a
    flag to refuse the dataset outright. Reading them (`lakehouse.features`) catches the whole
    class at once, including shapes with no observable consequence yet: `add_bases` registers a base
    path that no DataFile resolves through, so a referenced file is listed under the root after all
    and only the flag tells (measured). The listing's own check (:class:`_ReferencedElsewhere`) is the
    broader of the two: it also catches a dataset whose files do not all live here for a reason no
    flag records. The mirror hazard is invisible from here: the SOURCE of a clone holds files that only
    the CLONE's manifest still references, and the source carries no flag at all.

    **Branches.** A branch is a shallow clone of its parent whose `_versions/`, `_transactions/`,
    `_deletions/` and `_indices/` live under `tree/{branch}/`. `lance.dataset(uri)` opens the MAIN
    branch, so nothing under `tree/` is ever in the referenced set — every file of every branch is
    unreferenced BY CONSTRUCTION. Measured: a two-commit branch made 6 of 7 findings branch files,
    including the branch's own `data/*.lance`. Branching sets NO feature flag, so the directory probe
    is the only thing that sees it.

    The two directory probes are the only questions the scan asks the store about a key, and neither names a file a
    version references: maintenance's own store session reads no data, deletion or index file, so a HEAD of one is
    refused (rask's canned policy for maintenance, [[LH-575]]).
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
    return None


def _classify_against_floor(orphans: list[OrphanFile], ds: lance.LanceDataset, dataset_uri: str) -> None:
    """Stamp each orphan with whether `cleanup_old_versions` can still LIST it.

    CALLED ONLY WHEN SOMETHING IS ORPHANED, which is why it is a second pass rather than work inside
    the listing loop. `version_refs()` is used above precisely because it answers without
    deserializing a manifest (18.4x, per its own comment) and `versions()` is the opposite — it reads
    all of them. Most datasets have no orphans, and those pay nothing.

    THE FLOOR is the EARLIEST RETAINED manifest's commit timestamp, and it binds only the directories
    Lance lists through it (lance v13.0.0 `cleanup.rs:756-778`): `_transactions/` and `_deletions/`
    are clamped to it BEFORE the 7-day unverified rule filters what was listed, so a file of theirs
    above this instant is invisible to cleanup at any `older_than` and with `delete_unverified=True`
    (measured against pylance 11.0.0, the flip is at the manifest timestamp to the second). `data/` is
    listed through `max(floor, now - 7 days)` (lance #8944) and `_indices/` with no cutoff, so an
    orphan there is Lance's whatever its mtime, and the floor is not read for a dataset holding only
    those (:data:`_LISTED_PAST_THE_FLOOR_KINDS`).

    BOTH SIDES GO THROUGH `.timestamp()`, and that is the care this function takes. Measured on this
    host: `versions()[i]["timestamp"]` is NAIVE LOCAL (`2026-09-19 00:04:01`) while
    `pyarrow.fs.FileInfo.mtime` is TZ-AWARE UTC (`2026-09-18 22:04:01+00:00`) — the same instant
    written two hours apart. Comparing the datetimes raises on the mix, and stamping UTC onto the naive
    one to silence that misclassifies every file within the host's offset of a commit, one way only.

    A LISTED MTIME MAY BE A WHOLE SECOND while the floor keeps its fraction. pyarrow's S3 listing answered
    `1792418837.0` for a file boto3 read at `1792418837.942` on RustFS 1.0.3, against a floor of
    `1792418837.559`, and a moto server lists whole seconds through both clients. Such an mtime places the file
    anywhere in that second, so it is Lance's only when the whole second lies at or below the floor
    (:func:`listed_at_or_below`, which the listing-floor pass asks too); a file the scan cannot place falls to the floor-bound class, which the
    listing-floor pass resolves.

    An unreadable floor leaves the class None. "We could not tell" and "Lance will take it" must stay
    distinguishable, for the same reason `checked=False` is not `orphans=[]`.
    """
    floor_bound = [orphan for orphan in orphans if orphan.kind not in _LISTED_PAST_THE_FLOOR_KINDS]
    for orphan in orphans:
        if orphan.kind in _LISTED_PAST_THE_FLOOR_KINDS:
            orphan.reclaimable_by_lance = True
    if not floor_bound:
        return
    try:
        floor = min(committed_at(v).timestamp() for v in ds.versions())
    except Exception as exc:  # noqa: BLE001 — an unreadable floor leaves the class UNKNOWN, never guessed
        log.warning("orphan_floor_unreadable", extra={"dataset": dataset_uri, "error": str(exc)})
        return
    for orphan in floor_bound:
        orphan.reclaimable_by_lance = listed_at_or_below(orphan.mtime_epoch, floor)


def listed_at_or_below(mtime_epoch: float, floor: float) -> bool:
    """Whether a file listed at ``mtime_epoch`` was surely modified at or before ``floor``.

    A whole-second mtime may be a listing that dropped the fraction, so the file's own mtime lies anywhere in
    ``[mtime_epoch, mtime_epoch + 1)`` and only the end of that second decides.
    """
    latest = mtime_epoch + 1 if mtime_epoch.is_integer() else mtime_epoch
    return latest <= floor


def surely_before(mtime_epoch: float, instant: float) -> bool:
    """Whether a file listed at ``mtime_epoch`` was surely modified before ``instant``: a whole-second mtime may be a
    listing that dropped the fraction, so the file's own mtime lies anywhere in that second."""
    return mtime_epoch + 1 <= instant if mtime_epoch.is_integer() else mtime_epoch < instant


def scan_dataset(fs: pafs.FileSystem, dataset_uri: str, *, prefix: str, storage_options: StorageOptions | None = None) -> DatasetOrphanScan:
    """List one dataset's files and subtract what any live version references.

    ``prefix`` is the dataset's path as the filesystem sees it (no scheme), because pyarrow lists by
    path while Lance opens by URI — and it is KEYWORD-ONLY because it is a second `str` beside
    ``dataset_uri`` that very often carries the SAME value, which is precisely the pair a call site
    cannot self-check (MAINT-16).

    A dataset that cannot be READ yields ``checked=False`` and NO orphans. That distinction is the
    whole safety property: "we could not determine the referenced set" must never render as "none of
    these files are referenced", which is the shape that would delete a live table.

    A file the store cannot place before the instant the referenced set began to be read is no finding: a commit
    landing while the scan reads writes its files after that instant, and they look unreferenced. Age is the only
    evidence an unverified file has (`lance_docs/lance/guide/read_and_write.md:564-571`), so a file is an orphan only
    when its mtime, listed to the whole second or not, lies surely before that instant (:func:`surely_before`).
    """
    read_at = time.time()
    try:
        ds = open_dataset(dataset_uri, storage_options)
        referenced, versions = referenced_paths_of(ds, dataset_uri)
        if (files := sum(1 for rel in referenced if not rel.endswith("/"))) > _MAX_REFERENCED_FILES:
            raise _FilesPastTheCeiling(dataset_uri, files)
    except (_HistoryPastTheCeiling, _FilesPastTheCeiling) as exc:
        # Exclusions that arrive as exceptions because the read is where they become visible: a history by its
        # length, a referenced set by its size. Their own clause, so they are not sorted with the opens that merely
        # failed.
        log.warning("orphan_scan_skipped", extra={"dataset": dataset_uri, "reason": str(exc)})
        return DatasetOrphanScan(dataset=dataset_uri, checked=False, reason=str(exc), excluded=True)
    except Exception as exc:
        # pylance refuses a manifest whose feature flags IT does not know (measured: a committed data
        # overlay, flag 64) — a REFUSAL, and it must read as one. Reported raw it is a Rust source
        # path from a GitHub CI runner, which tells the report's reader nothing about why. An
        # ordinary unopenable path keeps its existing `TypeName: message` shape.
        refusal = unsupported_features_from_open_error(exc)
        reason = f"{prefix}: {refusal}" if refusal is not None else f"{type(exc).__name__}: {exc}"
        log.warning("orphan_scan_unreadable", extra={"dataset": dataset_uri, "error": str(exc)})
        # A flag refusal is STRUCTURAL on this path too: the reader will refuse the same manifest on
        # every future tick, so blocking reclamation on it never resolves. An open that failed for any
        # OTHER reason is a scan that could succeed later and must keep blocking.
        return DatasetOrphanScan(dataset=dataset_uri, checked=False, reason=reason, excluded=refusal is not None)

    if (unscannable := _unscannable_reason(ds, fs=fs, prefix=prefix)) is not None:
        log.warning("orphan_scan_skipped", extra={"dataset": dataset_uri, "reason": unscannable})
        # STRUCTURAL: the orphan method does not apply to this dataset's layout, so nothing was missed
        # and this must not block reclamation. Every other `checked=False` below TRIED and failed.
        return DatasetOrphanScan(dataset=dataset_uri, checked=False, versions_scanned=versions, reason=unscannable, excluded=True)

    try:
        orphans = _unreferenced(_listed(ds), referenced, dataset_uri, read_at=read_at)
    except (_ListingPastTheCeiling, _ReferencedElsewhere) as exc:
        log.warning("orphan_scan_skipped", extra={"dataset": dataset_uri, "reason": str(exc)})
        return DatasetOrphanScan(dataset=dataset_uri, checked=False, versions_scanned=versions, reason=str(exc), excluded=True)
    except Exception as exc:
        log.warning("orphan_listing_failed", extra={"dataset": dataset_uri, "error": str(exc)})
        return DatasetOrphanScan(dataset=dataset_uri, checked=False, versions_scanned=versions, reason=f"listing failed: {exc}")
    _classify_against_floor(orphans, ds, dataset_uri)
    return DatasetOrphanScan(dataset=dataset_uri, checked=True, orphans=orphans, versions_scanned=versions)


def _listed(ds: lance.LanceDataset) -> Iterator[tuple[str, int, float]]:
    """Every file under the dataset's root as ``(path relative to the root, size, mtime epoch)``, off Lance's own
    ``all_files()``.

    Read a batch at a time, so a listing past the ceiling is abandoned unread: measured on pylance 13.0.0 over 30,000
    files, the first 4,000-row batch arrived in 14 ms of a 107 ms listing.
    """
    for batch in ds.all_files():
        paths, sizes, modified = (batch.column(name).to_pylist() for name in ("path", "size_bytes", "last_modified"))
        for path, size, at in zip(paths, sizes, modified, strict=True):
            yield path, size or 0, at.timestamp() if at is not None else 0.0


def _unreferenced(listed: Iterator[tuple[str, int, float]], referenced: set[str], dataset_uri: str, *, read_at: float) -> list[OrphanFile]:
    """The listed files no live version references and the store places surely before ``read_at``.

    The listing is also what says every referenced file lives here: one it lacks resolves under another root, so the
    scan asks the store about no referenced file by its key.

    Raises:
        _ListingPastTheCeiling: more than :data:`_MAX_LISTED_FILES` files were listed.
        _ReferencedElsewhere: a referenced file was not listed.
    """
    orphans: list[OrphanFile] = []
    listed_referenced: set[str] = set()

    # Every referenced DIRECTORY — a data file's blob sidecar, an index segment — matched by prefix
    # rather than exactly, because what is referenced is the directory and its whole contents.
    #
    # Materialized ONCE, outside the loop: filtering the whole referenced set again for every listed file that is not an
    # exact match is O(files x referenced) Python-level work per dataset (the audit's HOUSE-RULE-16 addendum).
    referenced_dirs = tuple(d for d in referenced if d.endswith("/"))

    for count, (rel, size, mtime) in enumerate(listed, start=1):
        if count > _MAX_LISTED_FILES:
            raise _ListingPastTheCeiling(dataset_uri)
        if rel in referenced:
            listed_referenced.add(rel)
            continue
        # ...or it sits under a referenced directory: a data file's blob sidecar, an index segment.
        if rel.startswith(referenced_dirs):
            continue
        # Manifests and the hint are the VERSION INDEX itself, not payload: a manifest is what makes a
        # version live, so it can never be "unreferenced" while it is present, and the hint is
        # explicitly disposable. Reporting either every run would bury the findings that matter.
        if rel.startswith(f"{_VERSIONS_DIR}/") or rel.startswith(f"{_REFS_DIR}/") or rel == _RESERVED_MARKER:
            continue
        if not surely_before(mtime, read_at):
            continue
        orphans.append(OrphanFile(dataset=dataset_uri, path=rel, kind=_kind_of(rel), size_bytes=size, mtime_epoch=mtime))
    if missing := {rel for rel in referenced if not rel.endswith("/")} - listed_referenced:
        raise _ReferencedElsewhere(dataset_uri, missing)
    return orphans
