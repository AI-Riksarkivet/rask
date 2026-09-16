"""#75 on-demand garbage-collection endpoints — preview (dry-run reclaimable versions) + run, per table.

Both are owner-gated by the router (``maintenance/preview`` / ``maintenance/run`` → ``can_drop`` in
fga_deps — reclaiming version history is the drop rung, exactly like the retention policy that schedules it).
The preview never mutates; the run reclaims old versions with the sweep's tag exemption. The heavy Lance
work (open dataset, list versions, cleanup) runs in a threadpool so the event loop stays free.

**EVERY DOOR HERE DECLARES ``branch`` ONLY SO IT CAN REFUSE IT**, and not accepting the parameter is
not the same as refusing it: FastAPI drops an undeclared query parameter in silence, so a door that
"takes no branch" is exactly the shape that answers 200 for a branch it ignored. None of these verbs
can be scoped to a ref — ``open_dataset`` resolves main and nothing downstream carries one — so a
caller who names a branch and is told 200 has been told their branch was reclaimed, rewritten or
reindexed when MAIN was.

The estate has paid for this twice. ``indices.py`` records why the spec index doors declare the field
only to refuse it, and ``ff9604be`` fixed the destructive version: a branch-targeted
``drop_table_index`` was destroying MAIN's index and answering 200. The reindex door added on
2026-09-15 reintroduced the pattern on a new route the same day, which is why the gate
(``test_the_maintenance_doors_refuse_a_branch_they_cannot_honour``) is derived from the mounted routes
rather than written door by door — a fifth verb inherits it without an edit.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.concurrency import run_in_threadpool

from catalog.api import lineage_deps
from catalog.api.dependencies import LineageEmitterDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.core.identifiers import parse_identifier
from catalog.core.lineage_emit import COMPACT_TABLE, CREATE_INDEX
from catalog.core.namespace import open_dataset
from catalog.schemas import CompactAccepted, CompactRequest, CompactResult, GcPreview, GcRequest, GcRunResult, ReindexAccepted, ReindexRequest, ReindexResult
from catalog.services import dataplane, index_specs, maintenance
from service_kit import dapr_publish
from service_kit.lakehouse import base_refs
from service_kit.lakehouse.work_items import DatasetPlan, DatasetWorkItem, IndexWorkItem


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
async def preview_maintenance(id: str, body: GcRequest, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep, branch: str | None = None) -> GcPreview:
    """Dry-run the old-version cleanup — the versions GC would reclaim + the tags protecting others. Owner-
    gated (``can_drop``); never mutates.

    ``branch`` is DECLARED only so it can be REFUSED — see the module header."""
    dataplane.refuse_a_branch_this_door_cannot_honour(branch, door="maintenance/preview")
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
async def run_maintenance(id: str, body: GcRequest, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep, branch: str | None = None) -> GcRunResult:
    """Reclaim old versions on demand (DESTRUCTIVE; tag-pinned versions are exempt). Owner-gated
    (``can_drop``) — the same bar as scheduling it via the retention policy.

    ``branch`` is DECLARED only so it can be REFUSED — see the module header."""
    dataplane.refuse_a_branch_this_door_cannot_honour(branch, door="maintenance/run")
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


@router.post(
    "/{id}/maintenance/compact",
    # BOTH outcomes are declared, because this OpenAPI is a wire contract: it is generated into
    # `frontend/packages/api/src/generated/catalog.ts`, and a 202 the schema does not describe is a
    # response the typed client cannot name. The union return annotation carries the 200; `responses`
    # carries the 202, which FastAPI cannot infer from a status code set at runtime.
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": CompactAccepted,
            "description": "Enqueued onto the maintenance work queue; no fragment counts exist yet.",
        }
    },
)
async def compact_maintenance(
    id: str,
    body: CompactRequest,
    request: Request,
    response: Response,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
    branch: str | None = None,
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
    dataplane.refuse_a_branch_this_door_cannot_honour(branch, door="maintenance/compact")
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
            # The door's own request path IS the identity, so it never needs deriving. This is the
            # producer that most needs to supply it: a catalog-created table's location may be a
            # medallion path no parser can read back.
            table_id=id,
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
    # The QUEUED lane above is answered by an executor that emits; this lane has no such partner, and it
    # is the lane the deployed estate runs (`maintenance.workTopic` is empty on the release's values).
    # `pin_version` is None because `compact_now` reports fragment counts and no version — the trailer
    # reads the snapshot the rewrite just committed. `branch` is not threaded: this door refuses one above.
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=COMPACT_TABLE,
        authorization=authorization,
    )
    return CompactResult(**result)


@router.post(
    "/{id}/maintenance/reindex",
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": ReindexAccepted,
            "description": "Enqueued onto the index lane; the rebuilt version does not exist yet.",
        }
    },
)
async def reindex_maintenance(
    id: str,
    body: ReindexRequest,
    request: Request,
    response: Response,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
    branch: str | None = None,
) -> ReindexResult | ReindexAccepted:
    """Rebuild one named index in place ([[LH-105]]). Owner-gated (``can_drop``) — it destroys the
    index that is there, and an unmapped suffix would fall through to the writer rung.

    **IT REPLACES; IT DOES NOT DROP AND RECREATE**, which is the whole design and was measured rather
    than assumed. `LanceDataset.create_index` carries ``replace: bool = False`` and
    `create_scalar_index` carries ``replace: bool = True`` (pylance 11.0.0, 2026-09-15): a same-name
    vector rebuild is refused at the default — ``LanceError(Index): Index name 'x' already exists`` —
    and accepted under ``replace=True``. So the vector index, the one that cannot repair itself
    through the create doors, is repaired by a flag pylance already has. Dropping first would open a
    window in which the table has NO index — a search silently degrading to a full scan — and would
    leave it with none if the rebuild then failed, which is worse than the mis-parameterised index
    being repaired.

    **THE SHAPE IS READ, NOT RESTATED.** `index_specs.describe_index_for_rebuild` reads the live
    index's own parameterisation, so a repair cannot quietly re-tune what it repairs; `body.params`
    merges OVER that reading for the caller who is deliberately changing something.

    WHERE IT RUNS follows the compact door beside it, off the same topic name the maintenance service
    reads, so the two cannot disagree about whether a worker exists. With a queue: publish one
    `IndexWorkItem` and answer 202. Without one: nothing would ever execute the unit, so the rebuild
    runs here.
    """
    dataplane.refuse_a_branch_this_door_cannot_honour(branch, door="maintenance/reindex")
    segments = parse_identifier(id, settings.delimiter)
    ds = await run_in_threadpool(open_dataset, ns, so, segments)
    spec = await run_in_threadpool(index_specs.describe_index_for_rebuild, ds, body.index_name)
    params = {**spec.params, **body.params}

    publisher = getattr(request.app.state, "dapr_client", None)
    item = IndexWorkItem(
        uri=str(getattr(ds, "uri", "") or ""),
        table_id=id,
        column=spec.column,
        kind=spec.kind,
        index_type=spec.index_type,
        name=spec.name,
        replace=True,
        params=params,
    )
    if settings.maintenance_index_topic and publisher is not None:
        await dapr_publish.publish_event(
            publisher,
            timeout_seconds=settings.control_emit_timeout_seconds,
            pubsub_name=settings.maintenance_index_pubsub,
            topic_name=settings.maintenance_index_topic,
            data=item.model_dump_json(),
            data_content_type="application/json",
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return ReindexAccepted(
            index_name=spec.name,
            column=spec.column,
            kind=spec.kind,
            index_type=spec.index_type,
            params=params,
            transaction_id=item.unit_id,
        )

    outcome = await run_in_threadpool(maintenance.rebuild_index_now, ds, item)
    # `CREATE_INDEX` because a replace IS the create door's commit under another name — the two spec index
    # doors emit it, and a rebuild that reported nothing would leave the graph claiming the index still
    # dates from whichever build last went through `indices.py`. The version is in hand here, so it is
    # pinned rather than re-read.
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=CREATE_INDEX,
        pin_version=outcome,
        authorization=authorization,
    )
    return ReindexResult(index_name=spec.name, column=spec.column, kind=spec.kind, index_type=spec.index_type, version=outcome)
