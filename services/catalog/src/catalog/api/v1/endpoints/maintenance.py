"""The per-table maintenance doors: #75 on-demand garbage collection, and the tables maintenance has found due.

``maintenance/preview`` and ``maintenance/run`` are owner-gated by the router (``can_drop`` in ``fga_deps``: reclaiming
version history is the drop rung, like the retention policy that schedules it). The preview never mutates; the run
reclaims old versions with the sweep's tag exemption. Both honour ``branch``: a branch keeps its own ``_versions``,
``_transactions`` and ``data`` under ``tree/{branch}/`` (``lance_docs/file_format.md``), so a reclaim through a branch
handle deletes only the branch's own files (measured on pylance 11.0.0: the parent's ``data/`` stayed byte-identical
and still time-travelled), and an answer about one ref is never reported as another's.

``GET /management/v1/maintenance/due`` lists the tables a compaction or an index optimize is due on, as the
maintenance sweep wrote them (ADR 0162), to the callers that may maintain each. A bring-your-own job pulls it,
rewrites on its own compute, and commits through ``version/create``, which judges the result.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool

from catalog.api.dependencies import DoorTableDep, FgaClientDep, SettingsDep, StorageOptionsDep
from catalog.api.pagination import paginate
from catalog.api.security import CurrentToken
from catalog.core.base_judge import BaseJudge
from catalog.core.config import Settings
from catalog.schemas import DueTable, GcPreview, GcRequest, GcRunResult, MaintenanceDueResponse
from catalog.services import maintenance, table_claims
from service_kit.governed import fga
from service_kit.lakehouse import maintenance_due
from service_kit.lakehouse.history_fences import HistoryWork


log = logging.getLogger(__name__)

#: rask's own operations, not Lance namespace ones, so they mount under `/management/v1` ([[LH-021]]) and inherit the
#: three rungs of `api/v1/router.py`.
table_router = APIRouter(prefix="/management/v1/table", tags=["maintenance"])
due_router = APIRouter(prefix="/management/v1/maintenance", tags=["maintenance"])


async def _base_refs(ds: object, so: dict[str, str], settings: Settings) -> maintenance.BaseRefs:
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
    # Judged against the record and the configured bases ([[LH-279]]): a base a writer planted in a
    # sibling's manifest protects nothing, so it cannot freeze the table this button maintains.
    refs = await run_in_threadpool(BaseJudge.from_settings(settings).sibling_base_refs, location, so)
    if refs.unreadable:
        log.warning("maintenance_base_refs_incomplete", extra={"location": location, "unreadable": len(refs.unreadable)})
    return refs


@table_router.post("/{id}/maintenance/preview")
async def preview_maintenance(
    id: str,
    body: GcRequest,
    table: DoorTableDep,
    so: StorageOptionsDep,
    branch: Annotated[str | None, Query(description="The ref to preview. Omit for main; this door reads the ref it is given.")] = None,
) -> GcPreview:
    """Dry-run the old-version cleanup — the versions GC would reclaim + the tags protecting others. Owner-
    gated (``can_drop``); never mutates.

    ``branch`` IS HONOURED, as on every door here (see the module header), and this is the one that
    needs no containment argument: it calls ``_base_refs`` zero times and mutates nothing —
    ``preview_gc`` reads ``ds.version``, ``ds.versions()`` and the tag and child-branch pins and returns.

    Previewing MAIN and labelling it the branch's answer is the failure this replaces, not a lesser
    version of it: the caller acts on the version list, so
    ``test_the_gc_preview_names_exactly_what_the_run_deletes.py`` compares the ANSWER, per ref, with
    what ``/run`` deletes on that ref rather than asserting which ref the door opened."""
    ds = await run_in_threadpool(partial(table.open, so, branch=branch))
    result = await run_in_threadpool(
        maintenance.preview_gc,
        ds,
        branch=branch,
        retention_days=body.retention_days,
        retain_versions=body.retain_versions,
    )
    return GcPreview(**result)


@table_router.post("/{id}/maintenance/run")
async def run_maintenance(
    id: str,
    body: GcRequest,
    table: DoorTableDep,
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
    parent's data files, counted before and after — rather than which ref the door opened."""
    ds = await run_in_threadpool(partial(table.open, so, branch=branch))
    protected = await _base_refs(ds, so, settings)

    def _reclaim() -> maintenance.GcRunData:
        # [[LH-308]] Held while Lance deletes: it reads the refs once, so a tag or branch written meanwhile could pin a
        # version this deletes. A ref write holding it answers this request 409.
        with table.history_held(HistoryWork.RECLAIM):
            return maintenance.run_gc(ds, retention_days=body.retention_days, retain_versions=body.retain_versions, protected=protected, branch=branch)

    return GcRunResult(**await run_in_threadpool(_reclaim))


@due_router.get("/due")
async def list_due_maintenance(
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    page_token: Annotated[str | None, Query(description="Continue after this table id (the previous page's `page_token`).")] = None,
) -> MaintenanceDueResponse:
    """The tables a compaction or an index optimize is due on, in table order, listed to a caller holding
    ``can_maintain`` on each: what made each due, the policy record that governed it, and how many datasets resolve
    their files through it. A record names its table as the table is named now, which a rename since the sweep changed;
    one whose table is gone is listed to nobody, and the next sweep clears it."""
    records = await run_in_threadpool(maintenance_due.list_due, settings.registry_root, settings.storage_options())
    entries = [table_claims.TableRecord(location=record.location, uuid=record.uuid) for record in records]
    names = await run_in_threadpool(table_claims.names_now, table_claims.claim_store(settings), entries)
    named = [(name, record) for name, record in zip(names, records, strict=True) if name is not None]
    if settings.fga_enabled and token is not None and client is not None:
        # Checked on the uuid each record keeps ([[LH-524]]), which is the object the table's grants are held by.
        allowed = await fga.batch_check(
            client, user=token.subject, relation="can_maintain", objects=sorted({fga.table_object(record.uuid) for _, record in named})
        )
        named = [(name, record) for name, record in named if allowed.get(fga.table_object(record.uuid))]
    by_key = {f"{name}|{record.uuid}": (name, record) for name, record in named}
    page, next_token = paginate(sorted(by_key), page_token, limit)
    return MaintenanceDueResponse(tables=[_due_view(record, table=name) for name, record in (by_key[key] for key in page)], page_token=next_token)


def _due_view(record: maintenance_due.DueRecord, *, table: str) -> DueTable:
    """A record as the door shows it, naming its table ``table``: the referrers' locations are other tables' storage,
    so only their count."""
    return DueTable(
        table=table,
        uuid=record.uuid,
        version=record.version,
        compaction=record.compaction,
        indices=record.indices,
        policy=record.policy,
        clones=len(record.clones),
        evaluated_at=record.evaluated_at,
    )


# One exported router, as `router.py` mounts `_module.router` for every endpoint module.
router = APIRouter()
router.include_router(table_router)
router.include_router(due_router)
