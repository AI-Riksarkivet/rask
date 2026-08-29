"""#75 on-demand garbage-collection endpoints — preview (dry-run reclaimable versions) + run, per table.

Both are owner-gated by the router (``maintenance/preview`` / ``maintenance/run`` → ``can_drop`` in
fga_deps — reclaiming version history is the drop rung, exactly like the retention policy that schedules it).
The preview never mutates; the run reclaims old versions with the sweep's tag exemption. The heavy Lance
work (open dataset, list versions, cleanup) runs in a threadpool so the event loop stays free.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from catalog.api.dependencies import NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.core.identifiers import parse_identifier
from catalog.core.namespace import open_dataset
from catalog.schemas import CompactRequest, CompactResult, GcPreview, GcRequest, GcRunResult
from catalog.services import maintenance


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/table", tags=["maintenance"])


async def _base_refs(ds: object, so: dict[str, str]) -> maintenance.BaseRefs:
    """The #114 pre-pass, run BEFORE either destructive verb.

    It has to happen out here rather than inside the service function because the evidence is not on
    this dataset: a shallow clone's SOURCE carries no flag and no base_paths, and only the referring
    manifests say that anything resolves through its bytes. Collected per call rather than cached —
    a clone created a minute ago must protect its source on the next click, and the listing is one
    non-recursive call against a flat layout.

    An unreadable sibling is LOGGED and the call proceeds, matching the sweep
    (``maintenance_base_refs_incomplete``): a partial map still refuses everything it does see, and
    failing the button closed on any unreadable directory in the warehouse would make on-demand
    maintenance unusable. The refusals it can make are the point.
    """
    location = str(getattr(ds, "uri", "") or "")
    refs = await run_in_threadpool(maintenance.sibling_base_refs, location, so)
    if refs.unreadable:
        log.warning("maintenance_base_refs_incomplete", extra={"location": location, "unreadable": len(refs.unreadable)})
    return refs


@router.post("/{id}/maintenance/preview")
async def preview_maintenance(id: str, body: GcRequest, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep) -> GcPreview:
    """Dry-run the old-version cleanup — the versions GC would reclaim + the tags protecting others. Owner-
    gated (``can_drop``); never mutates."""
    segments = parse_identifier(id, settings.delimiter)
    ds = await run_in_threadpool(open_dataset, ns, so, segments)
    result = await run_in_threadpool(
        maintenance.preview_gc,
        ds,
        retention_days=body.retention_days,
        retain_versions=body.retain_versions,
    )
    return GcPreview(**result)


@router.post("/{id}/maintenance/run")
async def run_maintenance(id: str, body: GcRequest, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep) -> GcRunResult:
    """Reclaim old versions on demand (DESTRUCTIVE; tag-pinned versions are exempt). Owner-gated
    (``can_drop``) — the same bar as scheduling it via the retention policy."""
    segments = parse_identifier(id, settings.delimiter)
    ds = await run_in_threadpool(open_dataset, ns, so, segments)
    protected = await _base_refs(ds, so)
    result = await run_in_threadpool(
        maintenance.run_gc,
        ds,
        retention_days=body.retention_days,
        retain_versions=body.retain_versions,
        protected=protected,
    )
    return GcRunResult(**result)


@router.post("/{id}/maintenance/compact")
async def compact_maintenance(id: str, body: CompactRequest, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep) -> CompactResult:
    """Compact small fragments on demand (#76 'compact now'). Owner-gated (``can_drop``) — the same bar as
    the retention policy that schedules maintenance. Non-destructive: writes a new version, removes none."""
    segments = parse_identifier(id, settings.delimiter)
    ds = await run_in_threadpool(open_dataset, ns, so, segments)
    protected = await _base_refs(ds, so)
    result = await run_in_threadpool(
        maintenance.compact_now,
        ds,
        target_rows_per_fragment=body.target_rows_per_fragment,
        # The gate's base probe has to ask THIS dataset's store — see `require_compactable`. Handed
        # down rather than re-derived so the button and the sweep read the same bases the same way.
        storage_options=so,
        protected=protected,
    )
    return CompactResult(**result)
