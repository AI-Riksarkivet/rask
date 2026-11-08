"""The doors maintenance's drift reconciler reads and repairs the estate's grants through ([[LH-061]], [[XC-084]]): rask's
own operations, at ``/management/v1/grants``.

Maintenance holds no OpenFGA credential, since one could write any tuple (:mod:`catalog.services.grant_reconcile`). Its
drift report reads the store's grant census here, and its repair and rebuild ask the catalog to revoke the grants of an
object no registry names and to write a warehouse's tenancy edge its record justifies; the catalog judges each candidate
again on its own reads, and answers what it did and what it refused, with why. Every door asks ``can_reconcile_grants``
on the estate root, which only the ``reconciler`` role opens (the chart's bootstrap-admin hook seeds it for maintenance's
ServiceAccount, ``lance.fgaEstateRoles``).

The router guard leaves these paths to their handlers (``fga_deps._RESOURCES`` names no ``grants``), so the gate is this
router's own dependency, as the decision door's is.
"""

from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from catalog.api import fga_deps
from catalog.api.dependencies import FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.schemas import GhostRevokeRequest, GhostRevokeResponse, GrantCensusResponse, TenancyRebuildRequest, TenancyRebuildResponse
from catalog.services import grant_reconcile, warehouses
from lakehouse.governed import fga


async def require_can_reconcile_grants(client: FgaClientDep, settings: SettingsDep, token: CurrentToken) -> None:
    """Every grant door's gate: the caller holds ``can_reconcile_grants`` on the estate root."""
    await fga_deps.require_relation(client, settings, token, relation="can_reconcile_grants", obj=settings.fga_root_object)


router = APIRouter()
management_router = APIRouter(prefix="/management/v1/grants", tags=["grant-reconcile"], dependencies=[Depends(require_can_reconcile_grants)])


@management_router.get("/census")
async def grant_census(settings: SettingsDep, client: FgaClientDep) -> GrantCensusResponse:
    """Every object's grant count, by type, read whole from the store; the deployment's own objects left out.

    ``enabled`` is false when authorization is off in the catalog, which a drift report reads as nothing to check rather
    than as an estate holding no grants.
    """
    if not (settings.fga_enabled and client is not None):
        return GrantCensusResponse(enabled=False)
    counted = await fga.census(client, exclude=grant_reconcile.platform_objects(settings))
    return GrantCensusResponse(enabled=True, counts_by_type=counted.counts_by_type, truncated=counted.truncated)


@management_router.post("/ghosts/revoke")
async def revoke_ghost_grants(body: GhostRevokeRequest, settings: SettingsDep, client: FgaClientDep, token: CurrentToken) -> GhostRevokeResponse:
    """Revoke every grant on each object no registry names, as a drift report found them.

    Each object is judged again on the catalog's own reads (a project or warehouse record, a held location claim, a trash
    record) and one any of them names is refused, not revoked: a trashed table keeps its grants for its undrop.
    """
    held = await run_in_threadpool(grant_reconcile.held_table_uuids, settings)
    answer = GhostRevokeResponse()
    for obj in dict.fromkeys(body.objects):
        if (reason := await run_in_threadpool(partial(grant_reconcile.ghost_refusal, settings, obj, held=held))) is not None:
            answer.refused[obj] = reason
            continue
        answer.revoked[obj] = await fga_deps.revoke_ghost_object(client, settings, obj, token=token)
    return answer


@management_router.post("/tenancy/rebuild")
async def rebuild_warehouse_tenancy(body: TenancyRebuildRequest, settings: SettingsDep, client: FgaClientDep, token: CurrentToken) -> TenancyRebuildResponse:
    """Write each warehouse's tenancy edge, ``project:<p> project warehouse:<id>``, as its registry record names it.

    A structural edge confers nothing on its own, so rebuilding it re-grants no one; a warehouse whose record names no
    project, or no record at all, is answered unjustified.
    """
    answer = TenancyRebuildResponse()
    edges: list[fga.ClientTuple] = []
    for warehouse_id in dict.fromkeys(body.warehouses):
        record = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, settings.storage_options(), warehouse_id)
        match grant_reconcile.tenancy_edge(record, warehouse_id):
            case str() as reason:
                answer.unjustified[warehouse_id] = reason
            case edge:
                edges.append(edge)
    await fga_deps.write_registry_edges(client, settings, edges, token=token)
    answer.written = [f"{edge.user} {edge.relation} {edge.object}" for edge in edges]
    return answer
