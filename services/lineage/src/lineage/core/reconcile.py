"""Storage-version reconciliation (#23) — does the lineage graph agree with the actual Lance file?

Marquez and other catalogs are table-format-unaware: they record only what producers *emit*. Because we
own a Lance lakehouse, we can read the **actual on-disk version** and cross-check it against the version
the lineage graph recorded on the ``WROTE`` edge — and flag drift (a write that bypassed lineage, or a
lineage claim with no data behind it). This module is the pure core: a comparator + a thin Lance reader.
The endpoint that exposes it (gated on ``can_get_metadata``) wires these to the graph + storage config.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

import lance

from lineage.schemas import DatasetSummary, ReconcileState, ReconcileStatus
from service_kit.lakehouse import blobs
from service_kit.lakehouse.features import unsupported_features_from_open_error
from service_kit.lakehouse.schema import SchemaFields, facet_fields


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


def read_storage_version(uri: str, storage_options: dict[str, str]) -> int | None:
    """The current on-disk Lance version at ``uri`` — ``None`` ONLY when the dataset is genuinely absent.

    A missing dataset is a normal "no storage version" (it may not have been written yet), not an error.
    Anything else — an unsupported manifest feature flag, missing credentials, a bad endpoint or scheme,
    a pyo3 panic — raises :class:`StorageUnreadable`, because the caller classifies a `None` as
    MISSING_ON_STORAGE and reporting "we could not open it" as "it was destroyed" is a false alarm an
    operator acts on. Measured 2026-08-26: six live datasets reported as storage loss while
    ``services/maintenance`` was reading their manifests in the same hour.

    The absent marker is deliberately NARROW. pylance's wording for a genuinely missing dataset is
    "was not found"; a loose "not found" would also match S3's "Bucket 'x' not found", so a
    misconfigured endpoint would go on reading as a deleted dataset — the very bug this closes, one
    layer out.
    """
    try:
        return int(lance.dataset(uri, storage_options=storage_options).version)
    except BaseException as exc:
        _swallow_dataset_error(exc)
        if "was not found" in str(exc):
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
    async def backfill_write(self, name: str, version: int, schema: SchemaFields | None = None) -> None: ...


# The drift states that mean a real write's lineage event was LOST — storage has data the graph doesn't fully
# record. Only these are back-filled; GRAPH_AHEAD / MISSING_ON_STORAGE / IN_SYNC are not lost writes.
# Public: the cron route reports the same set, so there is ONE source of truth (no drift-prone duplicate).
BACKFILLABLE_STATES = (ReconcileState.STORAGE_AHEAD, ReconcileState.UNTRACKED)

# The drift states that mean STORAGE lost data the graph still records — the graph claims a version/dataset
# that on-disk Lance no longer has (e.g. an older PVC snapshot restored under the graph, a deleted dataset).
# These are NOT auto-fixable (we can't recreate lost data); the cron surfaces them as a WARNING so a bad
# restore / storage loss is visible instead of silently served as valid provenance.
STORAGE_LOSS_STATES = (ReconcileState.GRAPH_AHEAD, ReconcileState.MISSING_ON_STORAGE)


async def reconcile_all(
    repository: _ReconcileRepo,
    read_version: Callable[[str], Awaitable[int | None]],
    *,
    backfill: bool,
    read_schema: Callable[[str, int], Awaitable[SchemaFields | None]] | None = None,
    read_dangling: Callable[[str], Awaitable[list[str]]] | None = None,
    read_age: Callable[[str], Awaitable[float | None]] | None = None,
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
        uri = await repository.source_uri(summary.name)
        if uri is None:
            continue
        if await repository.dropped_at(summary.name):
            # Deliberately drop_table'd (terminal lifecycle stamp, 2026-07-11): absence on storage
            # is the EXPECTED state — sweeping it would WARN missing_on_storage forever on every
            # tick. A recreate clears the stamp on ingest and re-enters the sweep automatically.
            continue
        graph_version = await repository.latest_write_version(summary.name)
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
        # the CURRENT storage schema — a write that bypassed the mover skipped the gate; this doesn't.
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
        results.append(status)
    return results
