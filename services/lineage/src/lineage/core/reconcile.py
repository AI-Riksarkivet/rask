"""Storage-version reconciliation (#23) — does the lineage graph agree with the actual Lance file?

Marquez and other catalogs are table-format-unaware: they record only what producers *emit*. Because we
own a Lance lakehouse, we can read the **actual on-disk version** and cross-check it against the version
the lineage graph recorded on the ``WROTE`` edge — and flag drift (a write that bypassed lineage, or a
lineage claim with no data behind it). This module is the pure core: a comparator + a thin Lance reader.
The endpoint that exposes it (gated on ``can_get_metadata``) wires these to the graph + storage config.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final, Protocol

import lance

from lineage.schemas import DatasetSummary, ReconcileState, ReconcileStatus
from service_kit.lakehouse import blobs
from service_kit.lakehouse.features import unsupported_features_from_open_error
from service_kit.lakehouse.schema import SchemaFields, facet_fields
from service_kit.lancekit.absence import reads_as_absent


log = logging.getLogger(__name__)


def _swallow_dataset_error(exc: BaseException) -> None:
    """Treat any dataset-open/read failure as "unreadable" — EXCEPT real interpreter shutdown signals.

    ``except Exception`` alone is NOT enough here: lance's Rust boundary raises pyo3's
    ``PanicException`` for some malformed URIs / storage configs, and that derives from
    ``BaseException`` — so one bad dataSource URI would crash the WHOLE reconcile sweep (found by a
    real panic: ``RelativeUrlWithoutBase`` from the object-store crate, 2026-07-12). Re-raise only
    the signals that must never be swallowed.
    """
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        raise exc


class StorageUnreadable(Exception):
    """The dataset could not be OPENED — its state is unknown, which is not the same as gone."""


def _names_a_storage_location(uri: str) -> bool:
    """Could this string address storage at all? A scheme, or an absolute path.

    A RELATIVE uri is not a fact about the data. `lance.dataset("medallion/bronze")` answers "not
    found", which :func:`reads_as_absent` matches and the caller classifies MISSING_ON_STORAGE — so a
    dataset whose graph URI was recorded relative is reported DESTROYED on every tick, forever, because
    nothing rewrites it. Counted on the live graph 2026-09-11: 60 of 1162 Dataset nodes carry one,
    including one named `probe-relative-loc`.

    NOT RESOLVED AGAINST A ROOT, deliberately. A warehouse-bound table belongs to its warehouse's root,
    so joining against a configured one would turn an honest failure into a confident wrong answer. The
    repair needs an authoritative source and a place to live; classifying honestly needs neither.
    """
    return uri.startswith("/") or "://" in uri


def read_storage_version(uri: str, storage_options: dict[str, str]) -> int | None:
    """The current on-disk Lance version at ``uri`` — ``None`` ONLY when the dataset is genuinely absent.

    A uri that names no storage location at all — a RELATIVE path — raises before any open is attempted:
    it is a malformed name rather than evidence about the data, and the open would answer "not found".

    A missing dataset is a normal "no storage version" (it may not have been written yet), not an error.
    Anything else — an unsupported manifest feature flag, missing credentials, a bad endpoint or scheme,
    a pyo3 panic — raises :class:`StorageUnreadable`, because the caller classifies a `None` as
    MISSING_ON_STORAGE and reporting "we could not open it" as "it was destroyed" is a false alarm an
    operator acts on. Measured 2026-08-26: six live datasets reported as storage loss while
    ``services/maintenance`` was reading their manifests in the same hour.

    The absent marker is deliberately NARROW, and that reasoning is now the estate's rather than this
    module's: pylance's wording for a genuinely missing dataset is "was not found", while a loose
    "not found" also matches the `404 Not Found` status line every object-store error carries — so a
    misconfigured endpoint would go on reading as a deleted dataset, the very bug this closes one layer
    out. `service_kit.lancekit.absence` holds the vocabulary because three other seams asked the same
    question with wider lists and got the opposite answer for a missing bucket.
    """
    if not _names_a_storage_location(uri):
        raise StorageUnreadable(f"{uri!r} names no storage location — a relative path cannot say whether the data is there")
    try:
        return int(lance.dataset(uri, storage_options=storage_options).version)
    except BaseException as exc:
        _swallow_dataset_error(exc)
        if reads_as_absent(exc):
            return None
        reason = unsupported_features_from_open_error(exc) or f"{type(exc).__name__}: {exc}"
        raise StorageUnreadable(reason) from exc


def read_storage_schema(uri: str, storage_options: dict[str, str], version: int) -> SchemaFields | None:
    """The on-disk Lance column schema AT ``version`` as OpenLineage facet fields — ``None`` when unreadable.

    Used only when back-filling a lost write: the recovered WROTE edge then carries the per-version schema
    (#24). Pinned to the version being back-filled — an unpinned read would open the CURRENT snapshot, so a
    write landing between the version read and this one would stamp version N+1's columns onto the WROTE@N
    edge. Best-effort — a read failure yields ``None`` and the edge stays schemaless.
    """
    try:
        return facet_fields(lance.dataset(uri, storage_options=storage_options, version=version).schema)
    except BaseException as exc:
        _swallow_dataset_error(exc)
        return None


def read_storage_versions(uri: str, storage_options: dict[str, str]) -> list[int] | None:
    """Every version RETAINED on disk at ``uri``, ascending — ``None`` when the dataset is unreadable.

    The version axis compares two maxima, and a maximum cannot see a hole beneath it: a write whose
    lineage event was lost and which a later write then superseded leaves the graph's newest version
    equal to storage's, so the sweep reports ``in_sync`` and the back-fill never runs. Recovering that
    write needs the version SET, which is what this reads.

    ONE listing of the manifest directory, the same call :func:`read_latest_write_age_hours` already
    makes on the freshness axis — the cost is per dataset, never per version. Reclaimed versions are
    simply absent, which is why the comparison above this is one-directional (see :func:`reconcile_all`).

    ``None`` rather than ``[]`` for an unreadable or absent dataset: an empty list would read as "this
    dataset has no versions", and every version the graph holds would then look like storage loss.
    """
    try:
        return sorted(int(v["version"]) for v in lance.dataset(uri, storage_options=storage_options).versions())
    except BaseException as exc:
        _swallow_dataset_error(exc)
        return None


#: What a version reports when this binding cannot name its operation but the version provably changed
#: nothing. Spelled so it can never be mistaken for a Lance class name, because inventing one is the
#: mistake this whole classifier exists to stop making.
INERT_UNKNOWN: Final = "<inert>"

#: The per-version counters ``dataset.versions()`` already carries. Read from the call
#: :func:`read_storage_versions` makes anyway, so proving a version inert costs no extra I/O.
_COUNTER_KEYS: Final = ("total_rows", "total_data_files", "total_deletion_files", "total_deletion_file_rows")


def _version_counters(entry: dict[str, object]) -> tuple[str, ...] | None:
    """This version's data counters, or ``None`` when the manifest does not carry them."""
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict):
        return None
    present = tuple(str(metadata[key]) for key in _COUNTER_KEYS if key in metadata)
    return present if len(present) == len(_COUNTER_KEYS) else None


def read_version_operations(uri: str, storage_options: dict[str, str], versions: list[int]) -> dict[int, str | None]:
    """The Lance transaction OPERATION behind each of ``versions`` — ``None`` where it cannot be named.

    Read only for versions already found to be provenance holes, which on a healthy dataset is none: the
    cost is one transaction-file read per anomaly, never one per version per tick. One dataset open serves
    every hole, the same shape ``dataplane.table_history`` uses to answer the catalog's commit log.

    TWO THINGS READ AS UNNAMED, and neither may be branched on as though it were an operation.
    ``LanceOperation.BaseOperation`` is the ABC the twelve modelled operations inherit, so a concrete
    instance OF it means the Rust side committed something none of them covers — ``type(op).__name__``
    then yields the ABC's own name, which is the word "unknown" wearing an operation's clothes. An
    unreadable transaction (written before transaction files existed, or reclaimed) is unknown too. Both
    answer ``None``.

    EXCEPT WHEN THE VERSION IS PROVABLY INERT. Measured 2026-09-11, one ``compact_files()`` commits TWO
    versions — an unmodelled one whose counters are identical to its predecessor's, then the ``Rewrite``
    — so treating every unnamed version as reportable makes each compaction a permanent finding. A
    version whose ``total_rows``, data files and deletion counters all match the version below it wrote
    no data whatever its operation is called, and answers :data:`INERT_UNKNOWN`. Any counter moving, or a
    predecessor whose manifest is gone, keeps the honest ``None``.
    """
    try:
        dataset = lance.dataset(uri, storage_options=storage_options)
    except BaseException as exc:
        _swallow_dataset_error(exc)
        return dict.fromkeys(versions)
    try:
        counters = {int(entry["version"]): _version_counters(entry) for entry in dataset.versions()}
    except BaseException as exc:
        _swallow_dataset_error(exc)
        counters = {}
    operations: dict[int, str | None] = {}
    for version in versions:
        try:
            txn = dataset.read_transaction(version)
        except BaseException as exc:
            _swallow_dataset_error(exc)
            operations[version] = None
            continue
        op = getattr(txn, "operation", None)
        if op is None or type(op) is lance.LanceOperation.BaseOperation:
            here, below = counters.get(version), counters.get(version - 1)
            operations[version] = INERT_UNKNOWN if here is not None and here == below else None
            continue
        operations[version] = type(op).__name__
    return operations


def read_dangling_blob_columns(uri: str, storage_options: dict[str, str]) -> list[str]:
    """Blob-v2 columns at ``uri`` whose payloads no longer dereference — ``[]`` when healthy/no blobs.

    The reconcile half of the shared pointer-health probe (``service_kit.lakehouse.blobs`` — same probe the quality
    gate runs at promotion): the sweep re-checks the ALREADY-promoted estate, because an external
    object deleted after promotion (bucket wipe) changes no Lance version and so is invisible to the
    version comparison. Cheap by construction — a metadata open plus two 1-byte reads per blob
    column; a tabular dataset costs only the open. An unreadable dataset yields ``[]`` (not a
    finding): the version comparison already classifies missing/unreadable storage.
    """
    try:
        return blobs.dangling_blob_columns(lance.dataset(uri, storage_options=storage_options))
    except BaseException as exc:
        _swallow_dataset_error(exc)
        return []


def read_latest_write_age_hours(uri: str, storage_options: dict[str, str]) -> float | None:
    """Hours since the NEWEST version commit at ``uri`` — the freshness axis (data-contract gap #2).

    Read from STORAGE TRUTH (the version manifests' timestamps), not from the graph's event times —
    a write that bypassed lineage still counts as fresh data. Manifest timestamps are naive UTC
    (lance stamps commit time without a zone); clamped at 0 so clock skew can't yield negative age.
    ``None`` when unreadable/empty — the version comparison already classifies those.
    """
    try:
        versions = lance.dataset(uri, storage_options=storage_options).versions()
        if not versions:
            return None
        latest = max(v["timestamp"] for v in versions)
        return max((datetime.now(UTC) - latest.replace(tzinfo=UTC)).total_seconds() / 3600.0, 0.0)
    except BaseException as exc:
        _swallow_dataset_error(exc)
        return None


def reconcile(*, dataset: str, graph_version: int | None, storage_version: int | None, storage_unreadable: str | None = None) -> ReconcileStatus:
    """Compare the graph's recorded version against the on-disk version and classify any drift.

    ``storage_unreadable`` is checked FIRST and short-circuits every version comparison: if the
    dataset could not be opened there is no on-disk version to compare, and every other branch would
    be reasoning from an absence it mistook for a fact. Defaulting to ``None`` keeps every existing
    caller and case byte-identical.
    """
    if storage_unreadable is not None:
        return ReconcileStatus(
            dataset=dataset,
            graph_version=graph_version,
            storage_version=None,
            in_sync=False,
            status=ReconcileState.UNREADABLE,
            unreadable_reason=storage_unreadable,
        )
    if graph_version is None and storage_version is None:
        state = ReconcileState.ABSENT
    elif storage_version is None:
        state = ReconcileState.MISSING_ON_STORAGE
    elif graph_version is None:
        state = ReconcileState.UNTRACKED
    elif storage_version == graph_version:
        state = ReconcileState.IN_SYNC
    elif storage_version > graph_version:
        state = ReconcileState.STORAGE_AHEAD
    else:
        state = ReconcileState.GRAPH_AHEAD
    return ReconcileStatus(
        dataset=dataset,
        graph_version=graph_version,
        storage_version=storage_version,
        in_sync=state is ReconcileState.IN_SYNC,
        status=state,
    )


class _ReconcileRepo(Protocol):
    """The repository surface :func:`reconcile_all` needs (kept structural so the core stays testable)."""

    async def list_datasets(self, namespace: str | None = ..., tag: str | None = ...) -> list[DatasetSummary]: ...
    async def source_uri(self, name: str) -> str | None: ...
    async def dropped_at(self, name: str) -> str | None: ...
    async def latest_write_version(self, name: str) -> int | None: ...
    async def write_versions(self, name: str) -> set[int]: ...
    async def backfill_write(self, name: str, version: int, schema: SchemaFields | None = None) -> None: ...


# The drift states that mean a real write's lineage event was LOST — storage has data the graph doesn't fully
# record. Only these are back-filled; GRAPH_AHEAD / MISSING_ON_STORAGE / IN_SYNC are not lost writes.
# Public: the cron route reports the same set, so there is ONE source of truth (no drift-prone duplicate).
BACKFILLABLE_STATES = (ReconcileState.STORAGE_AHEAD, ReconcileState.UNTRACKED)


#: Lance transaction operations that PRESERVE what the table says, so a version carrying one is not
#: expected to have provenance and is not a hole.
#:
#: Grounded in pylance 11.0.0's own docstrings rather than inferred: ``Rewrite`` "rewrites one or more
#: files and indices into one or more files and indices" (compaction), ``CreateIndex`` "creates an index
#: on the dataset", ``UpdateConfig`` "updates dataset metadata". Every other operation Lance defines
#: changes what a reader sees — ``Restore`` "restores a previous version of the dataset", ``Project`` is
#: "drop column or rename/swap column", ``DataReplacement`` "replaces existing datafiles" — so each is a
#: write whose provenance must survive it.
#:
#: A DENYLIST RATHER THAN AN ALLOWLIST, and the direction is the whole point. An operation this estate has
#: never seen reads as a hole and gets REPORTED; an allowlist would drop it silently, and for a control
#: whose only job is finding missing provenance, failing silent is the one mode that cannot be tolerated.
#: An unnameable operation stays REPORTED — :func:`read_version_operations` answers ``None`` for it, and
#: ``None`` is not in this set. :data:`INERT_UNKNOWN` is the one narrowing of that: a version whose
#: counters match the version below it changed nothing, so it is not unknown in the sense that matters.
#: Without it every ``compact_files()`` is a permanent finding, because a compaction commits an
#: unmodelled version before its ``Rewrite``.
#:
#: WITHOUT THIS THE AXIS IS 20% NOISE, measured on the live estate 2026-09-11: of the 10 holes it found,
#: 8 were real data writes and 2 were a ``CreateIndex`` and a maintenance version on
#: ``transcripts_v2$annotations`` that never had provenance and never should. A finding an operator learns
#: to skim past is the failure mode this module already guards against elsewhere.
MAINTENANCE_OPERATIONS = frozenset({"Rewrite", "CreateIndex", "UpdateConfig", INERT_UNKNOWN})

#: The operations that WROTE data, and so the only ones a recovered edge may claim a run performed.
#:
#: REPORTING AND BACK-FILLING ARE DIFFERENT QUESTIONS, and collapsing them is what let a compaction plant
#: provenance. Reporting an unnameable version is honest and cheap to be wrong about — an operator looks
#: and moves on. Back-filling one writes ``(:Run)-[:WROTE]->(:Dataset)`` asserting a run wrote it, and
#: afterwards nothing in the graph distinguishes that fabrication from a real producer's event. So the
#: report keeps every unknown and the recovery takes only what can be NAMED as a data operation.
#:
#: Listed rather than derived from ``BaseOperation.__subclasses__()``: a Lance release adding an operation
#: should be a decision someone makes here, not something a dynamic lookup absorbs silently. A new data
#: operation is meanwhile reported and not recovered, which is the safe direction — a visible gap rather
#: than an invented run.
DATA_OPERATIONS = frozenset({"Append", "Overwrite", "Update", "Delete", "Merge", "Restore", "Project", "DataReplacement", "DataOverlay"})

#: How many provenance holes one dataset may have recovered in a single tick.
#:
#: A dataset the graph has never recorded a write for (UNTRACKED) has EVERY retained version as a hole,
#: so an uncapped recovery would make one tick's cost a function of the largest history in the estate —
#: the same unbounded-work argument that keeps compaction out of the catalog's request path. Capped, the
#: recovery converges over ticks instead, and the finding still names every hole it found. The report is
#: therefore the complete answer and the back-fill is the bounded one; :func:`reconcile_all` logs the
#: remainder rather than letting a truncation read as "recovered everything".
MAX_HOLES_BACKFILLED_PER_TICK = 25


async def reconcile_all(
    repository: _ReconcileRepo,
    read_version: Callable[[str], Awaitable[int | None]],
    *,
    backfill: bool,
    read_schema: Callable[[str, int], Awaitable[SchemaFields | None]] | None = None,
    read_dangling: Callable[[str], Awaitable[list[str]]] | None = None,
    read_age: Callable[[str], Awaitable[float | None]] | None = None,
    read_versions: Callable[[str], Awaitable[list[int] | None]] | None = None,
    read_operations: Callable[[str, list[int]], Awaitable[dict[int, str | None]]] | None = None,
    freshness_budget_hours: float = 0,
    declared: dict[str, list[str]] | None = None,
) -> list[ReconcileStatus]:
    """Reconcile every dataset the graph knows against storage; optionally back-fill dropped writes (B4).

    For each dataset carrying a dataSource URI, read the on-disk Lance version (via the injected
    ``read_version``, which the endpoint runs in a threadpool so object-store I/O never stalls the loop) and
    classify drift. When ``backfill`` and storage is AHEAD of — or UNTRACKED by — the graph (the outbox-gap
    signature), stamp the real version onto the graph and re-classify to in-sync. Read-only otherwise.
    ``read_schema`` (optional, same threadpool wrapping) recovers the on-disk column schema — called with
    the version being back-filled so the recovered schema is pinned to it — and rides the WROTE edge (#24).
    ``read_dangling`` (optional, same threadpool wrapping) probes blob-pointer health on datasets storage
    can actually read — the axis version comparison can't see (§9 P1 lifecycle) — and its findings ride
    ``dangling_blob_columns`` on the status; only run when a storage version exists (an unreadable dataset
    is already the version check's finding).
    """
    results: list[ReconcileStatus] = []
    for summary in await repository.list_datasets():
        # Issued TOGETHER, not one after another: three independent point lookups on the same graph,
        # none of which needs another's answer. Sequenced, they made every dataset three round-trips
        # deep, so a sweep over an estate of N datasets paid 3N serial round-trips. A dataset that
        # turns out to have no dataSource URI (or a drop stamp) now pays two key reads it would have
        # skipped — the cheapest possible lookups, against a depth cut from 3N to N.
        uri, dropped, graph_version = await asyncio.gather(
            repository.source_uri(summary.name),
            repository.dropped_at(summary.name),
            repository.latest_write_version(summary.name),
        )
        if uri is None:
            continue
        if dropped:
            # Deliberately drop_table'd (terminal lifecycle stamp, 2026-07-11): absence on storage
            # is the EXPECTED state — sweeping it would WARN missing_on_storage forever on every
            # tick. A recreate clears the stamp on ingest and re-enters the sweep automatically.
            continue
        # A dataset this reader cannot OPEN is classified UNREADABLE and skips every downstream axis
        # below: with no storage version there is nothing to compare, no blob pointer to probe, no
        # schema to read, and nothing to back-fill. Reporting it as loss is what sent an operator
        # looking for six datasets that were never gone.
        storage_unreadable: str | None = None
        storage_version: int | None = None
        try:
            storage_version = await read_version(uri)
        except StorageUnreadable as exc:
            storage_unreadable = str(exc)
        # THE TIP IS THE NEWEST DATA VERSION, not the newest version. A compaction commits versions on
        # top of the last write, so the raw maximum reads as drift the graph could never close: the
        # back-fill below would stamp a `WROTE` edge on a `Rewrite`, and the next tick would find the
        # same gap again. Resolved only when the two maxima actually disagree, so a healthy dataset pays
        # nothing and a drifting one pays one transaction read.
        if (
            storage_version is not None
            and (graph_version is None or storage_version > graph_version)
            and read_versions is not None
            and read_operations is not None
        ):
            storage_version = await _newest_data_version(
                uri,
                tip=storage_version,
                floor=graph_version,
                read_versions=read_versions,
                read_operations=read_operations,
            )
        status = reconcile(
            dataset=summary.name,
            graph_version=graph_version,
            storage_version=storage_version,
            storage_unreadable=storage_unreadable,
        )
        if storage_version is not None and read_dangling is not None:
            status.dangling_blob_columns = await read_dangling(uri)
        # Freshness (data-contract gap #2): only when a budget is configured AND storage is readable —
        # an unreadable dataset is already the version check's finding, and budget 0 means the axis is
        # off (no probe at all, so default deployments pay nothing).
        if freshness_budget_hours > 0 and storage_version is not None and read_age is not None:
            age = await read_age(uri)
            status.stale = age is not None and age > freshness_budget_hours
        # Declared-columns patrol (Batch 23): re-check the gate's column_declared assertion against
        # the CURRENT storage schema — a write that bypassed the stage runner skipped the gate; this doesn't.
        # Only declared datasets pay the schema read; a failed read reports nothing (the version
        # check already classifies unreadable storage; a phantom violation would cry wolf).
        wanted = (declared or {}).get(summary.name)
        if wanted and storage_version is not None and read_schema is not None:
            fields = await read_schema(uri, storage_version)
            if fields is not None:
                present = {f.get("name") for f in fields}
                status.missing_declared_columns = [c for c in wanted if c not in present]
        if backfill and storage_version is not None and status.status in BACKFILLABLE_STATES:
            # Fix the drift as a side effect but keep the found status in the report — a subsequent sweep
            # will show it in_sync, proving the back-fill took. The schema read is pinned to the version
            # being back-filled, so a write landing mid-sweep can't attach a later schema to this edge.
            schema = await read_schema(uri, storage_version) if read_schema is not None else None
            await repository.backfill_write(summary.name, storage_version, schema=schema)
        # Provenance holes BELOW the tip — the axis the two-maxima comparison above is blind to. Only
        # when storage is readable, on the same rule the freshness and declared-column axes follow: an
        # unreadable dataset is already the version check's finding.
        if read_versions is not None and storage_version is not None:
            status.versions_without_lineage = await _recover_holes(
                repository,
                summary.name,
                uri,
                read_versions=read_versions,
                read_operations=read_operations,
                read_schema=read_schema,
                backfill=backfill,
            )
        results.append(status)
    return results


#: How far below the tip the drift path will look for the newest version that wrote data.
#:
#: A compaction commits two versions, so a small window covers several stacked maintenance passes. The
#: bound exists because the walk is unbounded in principle and this is a per-dataset, per-tick cost;
#: exhausting it keeps the raw tip, which is exactly the behaviour that shipped before this, so the
#: degradation is to the status quo rather than to something new.
MAX_TIP_PROBE_VERSIONS = 8


async def _newest_data_version(
    uri: str,
    *,
    tip: int,
    floor: int | None,
    read_versions: Callable[[str], Awaitable[list[int] | None]],
    read_operations: Callable[[str, list[int]], Awaitable[dict[int, str | None]]],
) -> int:
    """The newest version at or below ``tip`` that WROTE data, walking down from the tip.

    ``floor`` is the graph's own tip and bounds the search: a version the graph already has an edge for
    is where the comparison converges, so returning it classifies the dataset in_sync — which is the
    point. Every version above the floor being maintenance means storage is not ahead at all.

    Falls back to ``tip`` when nothing can be resolved, never to a guess: an unreadable listing, an empty
    candidate window, or a probe that finds no data operation all leave the comparison exactly as it was.
    """
    on_disk = await read_versions(uri)
    if on_disk is None:
        return tip
    candidates = sorted((v for v in on_disk if v <= tip and (floor is None or v > floor)), reverse=True)[:MAX_TIP_PROBE_VERSIONS]
    if not candidates:
        return tip
    operations = await read_operations(uri, candidates)
    for version in candidates:
        if operations.get(version) in DATA_OPERATIONS:
            return version
    return floor if floor is not None else tip


async def _recover_holes(
    repository: _ReconcileRepo,
    name: str,
    uri: str,
    *,
    read_versions: Callable[[str], Awaitable[list[int] | None]],
    read_operations: Callable[[str, list[int]], Awaitable[dict[int, str | None]]] | None,
    read_schema: Callable[[str, int], Awaitable[SchemaFields | None]] | None,
    backfill: bool,
) -> list[int]:
    """Versions on disk the graph holds no WROTE edge for — back-filled when ``backfill``, always reported.

    ONE-DIRECTIONAL, and it has to be: ``cleanup_old_versions`` reclaims old manifests, so a maintained
    dataset legitimately has versions in the graph that storage no longer holds. Reading that direction as
    a finding would turn every compacted dataset in the estate permanently red, which is how an axis ends
    up switched off. Only ``on_disk - in_graph`` is a hole.

    The recovered provenance is the same minimal edge the tip back-fill writes — ``author='reconcile'``,
    no inputs — because that is all storage can supply: it records THAT the version was written and its
    schema, never who wrote it or what it derived from. That is a floor under the estate's provenance
    claim, not a replacement for the producer's own event.
    """
    on_disk = await read_versions(uri)
    if on_disk is None:
        return []
    holes = sorted(set(on_disk) - await repository.write_versions(name))
    if holes and read_operations is not None:
        # A compaction, an index build and a config change each commit a version and correctly emit no
        # lineage, so without this the axis reports every maintained dataset. Paid ONLY on the holes, which
        # is why the classifier can afford a transaction read at all.
        operations = await read_operations(uri, holes)
        holes = [v for v in holes if operations.get(v) not in MAINTENANCE_OPERATIONS]
    if not holes or not backfill:
        return holes
    # Every hole is REPORTED above; only a NAMED data operation is recovered. An unnameable version is a
    # gap the sweep can see and cannot explain, and a back-filled edge would answer it with a run that
    # never existed — indistinguishable from a real event once written.
    recoverable = [v for v in holes if operations.get(v) in DATA_OPERATIONS] if read_operations is not None else holes
    for version in recoverable[:MAX_HOLES_BACKFILLED_PER_TICK]:
        schema = await read_schema(uri, version) if read_schema is not None else None
        await repository.backfill_write(name, version, schema=schema)
    if len(recoverable) > MAX_HOLES_BACKFILLED_PER_TICK:
        # NAMED, never silent: a truncated recovery that logged nothing would report the full finding
        # while fixing part of it, and the next tick's smaller finding would read as progress nobody made.
        log.warning(
            "lineage_reconcile_holes_truncated",
            extra={"dataset": name, "holes": len(holes), "recoverable": len(recoverable), "recovered": MAX_HOLES_BACKFILLED_PER_TICK},
        )
    return holes
