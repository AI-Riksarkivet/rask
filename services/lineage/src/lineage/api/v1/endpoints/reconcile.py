"""Storage-version reconciliation endpoint (#23) — does the graph agree with the on-disk Lance file?

Our moat over format-unaware catalogs: because we own a Lance lakehouse we read
the real on-disk version and cross-check it against the version the graph recorded on the ``WROTE`` edge.
Gated on ``can_get_metadata`` for ``{name}`` (router-level); the blocking Lance read runs in the
threadpool so the object-store I/O never stalls the event loop.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from lineage.api.dependencies import RepositoryDep, SettingsDep
from lineage.api.fga_deps import audit_read, require_metadata_access
from lineage.core.config import storage_options
from lineage.core.reconcile import (
    read_dangling_blob_columns,
    read_latest_write_age_hours,
    read_storage_version,
    reconcile,
)
from lineage.schemas import ReconcileStatus


# Gate first (require_metadata_access), then log the authorized reconcile read (#6); deps run in order.
router = APIRouter(
    prefix="/datasets",
    tags=["query"],
    dependencies=[Depends(require_metadata_access), Depends(audit_read)],
)


@router.get("/{name}/reconcile")
async def get_reconcile(name: str, repository: RepositoryDep, settings: SettingsDep) -> ReconcileStatus:
    """Does the lineage graph agree with the **actual Lance file on storage**? (#23)

    Our moat over format-unaware catalogs: because we own a Lance lakehouse we
    read the real on-disk version and cross-check it against the version the graph recorded on the
    ``WROTE`` edge — surfacing a write that bypassed lineage (``storage_ahead``) or a lineage claim
    with no data behind it (``missing_on_storage``). Gated on ``can_get_metadata`` for ``name``; the
    Lance read runs in the threadpool so the blocking object-store I/O never stalls the event loop.
    """
    graph_version = await repository.latest_write_version(name)
    uri = await repository.source_uri(name)
    opts = storage_options(settings)
    storage_version = await run_in_threadpool(read_storage_version, uri, opts) if uri is not None else None
    status = reconcile(dataset=name, graph_version=graph_version, storage_version=storage_version)
    # Blob-pointer health (§9 P1 lifecycle) — invisible to the version comparison: an external payload
    # deleted after promotion changes no Lance version. Only probed when storage is readable at all.
    if uri is not None and storage_version is not None:
        status.dangling_blob_columns = await run_in_threadpool(read_dangling_blob_columns, uri, opts)
        # Freshness (gap #2): asserted only when a budget is configured — 0 keeps the axis off.
        if settings.freshness_budget_hours > 0:
            age = await run_in_threadpool(read_latest_write_age_hours, uri, opts)
            status.stale = age is not None and age > settings.freshness_budget_hours
    return status
