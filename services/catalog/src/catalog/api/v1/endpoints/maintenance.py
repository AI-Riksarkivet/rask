"""#75 on-demand garbage-collection endpoints — preview (dry-run reclaimable versions) + run, per table.

Both are owner-gated by the router (``maintenance/preview`` / ``maintenance/run`` → ``can_drop`` in
fga_deps — reclaiming version history is the drop rung, exactly like the retention policy that schedules it).
The preview never mutates; the run reclaims old versions with the sweep's tag exemption. The heavy Lance
work (open dataset, list versions, cleanup) runs in a threadpool so the event loop stays free.

**EVERY DOOR HERE DECLARES ``branch``, AND WHAT IT DOES WITH ONE IS PER DOOR.** Declaring it is not
optional either way: FastAPI drops an undeclared query parameter in silence, so a door that "takes no
branch" is exactly the shape that answers 200 for a branch it ignored.

ALL FOUR VERBS HONOUR IT, and each for a reason measured on the door rather than inferred from how
destructive it sounds. Reindex carries the ref on the work item so the worker opens what the request
named. Preview calls ``_base_refs`` zero times and mutates nothing. Compact is answered by
``require_compactable``, an evidence gate that refuses a real shallow clone on COST. Run reclaims, and
its reclaim is contained by the LAYOUT: a branch keeps its own ``_versions``/``_transactions``/``data``
under ``tree/{branch}/`` (``lance_docs/file_format.md:2746-2761``), so ``cleanup_old_versions`` through
a branch handle deletes the branch's own files. Measured on pylance 11.0.0, on a branch that had
overwritten every parent fragment — the sharpest case, where all of the parent's bytes are garbage from
the branch's point of view — one data file went and it was the branch's own; the parent's ``data/``
stayed byte-identical and still time-travelled to v1.

A branch handle reports the DATASET ROOT as its ``uri`` (measured: ``main.uri == branch.uri``), so
``_base_refs`` lists the same directory and ``is_protected`` checks the same location whichever ref the
request named. That is why ``/run``'s refusal bought nothing and was deleted: it cited a bound — one
parent listing, blind to a referrer under another root — that applies identically to the MAIN request
this door has always accepted.

A door that honours a ref must answer ABOUT that ref: previewing main and labelling it the branch's is
the same defect as reclaiming main, delivered as information instead of as deletion.

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

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool

from catalog.api import lineage_deps
from catalog.api.dependencies import LineageEmitterDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.core.identifiers import parse_identifier
from catalog.core.lineage_emit import COMPACT_TABLE, CREATE_INDEX
from catalog.core.namespace import open_dataset
from catalog.schemas import CompactAccepted, CompactRequest, CompactResult, GcPreview, GcRequest, GcRunResult, ReindexAccepted, ReindexRequest, ReindexResult
from catalog.services import index_specs, maintenance
from service_kit import dapr_publish
from service_kit.lakehouse import base_refs
from service_kit.lakehouse.work_items import DatasetPlan, DatasetWorkItem, IndexWorkItem


log = logging.getLogger(__name__)

#: THE MANAGEMENT SURFACE ([[LH-021]]). These are rask's own operations, not Lance namespace ones —
#: a spec client discovering them on `/v1/table` meets verbs the document never defines. They mount at
#: `/management/v1` and inherit the same authn/authz and delimiter guard from `api/v1/router.py`.
router = APIRouter(prefix="/management/v1/table", tags=["maintenance"])


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
async def preview_maintenance(
    id: str,
    body: GcRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    branch: Annotated[str | None, Query(description="The ref to preview. Omit for main; this door reads the ref it is given.")] = None,
) -> GcPreview:
    """Dry-run the old-version cleanup — the versions GC would reclaim + the tags protecting others. Owner-
    gated (``can_drop``); never mutates.

    ``branch`` IS HONOURED HERE while its two destructive siblings still refuse it, and the split is
    measured rather than stylistic: this door calls ``_base_refs`` zero times and mutates nothing —
    ``preview_gc`` reads ``ds.version``, ``ds.versions()`` and the tag and child-branch pins and returns — so [[LH-094]]'s
    question about what a reclaim may DELETE on a branch never reaches it. ``/run`` and ``/compact``
    both reclaim, and stay refused until that is decided for them.

    Previewing MAIN and labelling it the branch's answer is the failure this replaces, not a lesser
    version of it: the caller acts on the version list, so
    ``test_the_gc_preview_previews_the_ref_the_request_names`` compares the ANSWER between refs rather
    than asserting the branch reached ``open_dataset``."""
    segments = parse_identifier(id, settings.delimiter)
    ds = await run_in_threadpool(open_dataset, ns, so, segments, branch=branch)
    result = await run_in_threadpool(
        maintenance.preview_gc,
        ds,
        branch=branch,
        retention_days=body.retention_days,
        retain_versions=body.retain_versions,
    )
    return GcPreview(**result)


@router.post("/{id}/maintenance/run")
async def run_maintenance(
    id: str,
    body: GcRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    branch: Annotated[
        str | None, Query(description="The ref to reclaim. Omit for main; a reclaim through a branch handle is scoped to that branch's own files.")
    ] = None,
) -> GcRunResult:
    """Reclaim old versions on demand (DESTRUCTIVE; tag-pinned versions are exempt). Owner-gated
    (``can_drop``) — the same bar as scheduling it via the retention policy.

    ``branch`` IS HONOURED, and the containment is the LAYOUT's rather than this door's — see the
    module header for the measurement. The pre-pass below runs on both paths and is the same listing
    either way, because a branch handle's ``uri`` is the dataset root.

    Reclaiming MAIN's history and reporting it as the branch's is the failure this replaces, and it is
    irreversible, so ``test_the_gc_run_reclaims_the_ref_the_request_names`` asserts what SURVIVED — the
    parent's data files, counted before and after — rather than that the branch reached
    ``open_dataset``."""
    segments = parse_identifier(id, settings.delimiter)
    ds = await run_in_threadpool(open_dataset, ns, so, segments, branch=branch)
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
    branch: Annotated[
        str | None,
        Query(
            description="The ref to compact. A branch is answered by the evidence gate, which refuses on COST when fragments still resolve through the parent."
        ),
    ] = None,
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
    # NO SHAPE REFUSAL HERE. A branch is answered by `require_compactable`, which is evidence-based and
    # already refuses a real shallow clone on COST — "compacting one materialises the shared data into
    # its own root, 1,072 -> 108,199 bytes against a 119,693-byte base". A branch IS a shallow clone
    # (`file_format.md:2744`), so it reaches that gate and gets that answer, measured against THIS
    # dataset rather than asserted from its shape.
    #
    # The door used to refuse first, and its reason was wrong about the harm: measured on pylance
    # 11.0.0, compacting a branch with 3 inherited and 2 own fragments reported
    # `fragments_removed=5, fragments_added=1`, left main's data directory byte-identical at 3 files,
    # and both refs still read (branch 40 rows, main 30). So it DOES merge this ref's own fragments and
    # the parent is not endangered — the cost is duplication, which is exactly what the evidence gate
    # measures and this one could not. It also made the answer permanent: a branch that has already
    # been materialised owns all its fragments and is cheap to compact, and a shape refusal can never
    # notice that.
    segments = parse_identifier(id, settings.delimiter)
    ds = await run_in_threadpool(open_dataset, ns, so, segments, branch=branch)
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
    # reads the snapshot the rewrite just committed.
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
    branch: Annotated[
        str | None, Query(description="The ref to rebuild on. Omit for main; the ref travels with the work item so the worker opens what you named.")
    ] = None,
) -> ReindexResult | ReindexAccepted:
    """Rebuild one named index in place ([[LH-105]]). Owner-gated (``can_drop``, declared in
    ``fga_deps._OWNER_SUFFIX_RELATION``) — it destroys the index that is there.

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
    segments = parse_identifier(id, settings.delimiter)
    ds = await run_in_threadpool(open_dataset, ns, so, segments, branch=branch)
    spec = await run_in_threadpool(index_specs.describe_index_for_rebuild, ds, body.index_name)
    params = {**spec.params, **body.params}

    publisher = getattr(request.app.state, "dapr_client", None)
    item = IndexWorkItem(
        uri=str(getattr(ds, "uri", "") or ""),
        # THE REF TRAVELS WITH THE UNIT, and that is what lets this door accept a branch at all. It
        # publishes and answers 202, so the WORKER opens the dataset; a branch accepted here but not
        # carried would rebuild MAIN's index while this response reported the branch's.
        branch=branch or "",
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
