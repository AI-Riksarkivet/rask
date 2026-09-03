"""#75 on-demand garbage-collection endpoints — preview (dry-run reclaimable versions) + run, per table.

Both are owner-gated by the router (``maintenance/preview`` / ``maintenance/run`` → ``can_drop`` in
fga_deps — reclaiming version history is the drop rung, exactly like the retention policy that schedules it).
The preview never mutates; the run reclaims old versions with the sweep's tag exemption. The heavy Lance
work (open dataset, list versions, cleanup) runs in a threadpool so the event loop stays free.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response, status
from fastapi.concurrency import run_in_threadpool

from catalog.api.dependencies import NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.core.identifiers import parse_identifier
from catalog.core.namespace import open_dataset
from catalog.schemas import CompactAccepted, CompactRequest, CompactResult, GcPreview, GcRequest, GcRunResult
from catalog.services import maintenance
from service_kit import dapr_publish
from service_kit.lakehouse import base_refs
from service_kit.lakehouse.work_items import DatasetPlan, DatasetWorkItem


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
    refs = await run_in_threadpool(base_refs.sibling_base_refs, location, so)
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


@router.post("/{id}/maintenance/compact", response_model=None)
async def compact_maintenance(
    id: str,
    body: CompactRequest,
    request: Request,
    response: Response,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
) -> CompactResult | CompactAccepted:
    """Compact small fragments on demand (#76 'compact now'). Owner-gated (``can_drop``) — the same bar as
    the retention policy that schedules maintenance. Non-destructive: writes a new version, removes none.

    WHERE THE REWRITE HAPPENS depends on whether this deployment has a maintenance queue, and the two
    answers are not a feature flag — they are the same choice ``services/maintenance`` already makes for
    the scheduled lane, read off the same topic name so the two cannot disagree about whether a worker
    exists. With a queue: publish one unit, 202, done in milliseconds. Without one: nothing would ever
    execute that unit, so the rewrite runs here as it always has.

    What stays in the handler either way is the BOUNDED half — parsing the identifier, opening the
    dataset, and the ``sibling_base_refs`` pre-pass (one non-recursive listing). What leaves is the half
    whose cost is a property of the data rather than of the request: rewriting every fragment of a table
    whose fragment count nobody bounded.
    """
    segments = parse_identifier(id, settings.delimiter)
    ds = await run_in_threadpool(open_dataset, ns, so, segments)
    protected = await _base_refs(ds, so)

    publisher = getattr(request.app.state, "dapr_client", None)
    if settings.maintenance_work_topic and publisher is not None:
        location = str(getattr(ds, "uri", "") or "")
        item = DatasetWorkItem(
            uri=location,
            # The executor runs the full ordered pass (compact -> optimize_indices -> cleanup). This door
            # is documented non-destructive, so both later steps are OFF: moving the work to another lane
            # must not quietly turn "compact now" into "compact and reclaim history now".
            plan=DatasetPlan(
                target_rows_per_fragment=body.target_rows_per_fragment,
                cleanup_enabled=False,
                optimize_indices_enabled=False,
            ),
            protected_by=protected.is_protected(location),
        )
        await dapr_publish.publish_event(
            publisher,
            timeout_seconds=settings.control_emit_timeout_seconds,
            pubsub_name=settings.maintenance_work_pubsub,
            topic_name=settings.maintenance_work_topic,
            data=item.model_dump_json(),
            data_content_type="application/json",
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return CompactAccepted(uri=location, protected_by=item.protected_by)

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
