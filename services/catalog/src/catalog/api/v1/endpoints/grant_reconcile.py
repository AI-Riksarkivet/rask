"""The doors maintenance's drift reconciler reads and repairs the estate's grants through ([[LH-061]], [[XC-084]]): rask's
own operations, at ``/management/v1/grants``.

Maintenance holds no OpenFGA credential, since one could write any tuple (:mod:`catalog.services.grant_reconcile`). Its
drift report reads the store's grant census here, and its repair and rebuild ask the catalog to revoke the grants of an
object no registry names and to write a warehouse's tenancy edge its record justifies; the catalog judges each candidate
again on its own reads, and answers what it did and what it refused, with why. Every door asks ``can_reconcile_grants``
on the estate root, which only the ``reconciler`` role opens (the catalog's declaration grants it to maintenance's
ServiceAccount, ``lance.fgaEstateRoles``).

The router guard leaves these paths to their handlers (``rungs._RESOURCES`` names no ``grants``), so the gate is this
router's own dependency, as the decision door's is.
"""

from functools import partial

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import PermissionDeniedError

from catalog.api.authz import decisions, door_gates, object_grants
from catalog.api.authz.context import AuthzContextDep
from catalog.api.dependencies import FgaClientDep, SettingsDep, namespace_for_table
from catalog.core.config import Settings
from catalog.schemas import GhostRevokeRequest, GhostRevokeResponse, GrantCensusResponse, TenancyRebuildRequest, TenancyRebuildResponse
from catalog.services import grant_reconcile, table_claims, warehouses
from lakehouse.governed import fga_reads, fga_writes
from lakehouse.records import location_claims


async def require_can_reconcile_grants(context: AuthzContextDep) -> None:
    """Every grant door's gate: the caller holds ``can_reconcile_grants`` on the estate root."""
    await door_gates.require_relation(context, relation="can_reconcile_grants", obj=context.settings.fga_root_object)


management_router = APIRouter(prefix="/management/v1/grants", tags=["grant-reconcile"], dependencies=[Depends(require_can_reconcile_grants)])


@management_router.get("/census")
async def grant_census(settings: SettingsDep, client: FgaClientDep) -> GrantCensusResponse:
    """Every object's grant count, by type, read whole from the store; the deployment's own objects left out.

    ``enabled`` is false when authorization is off in the catalog, which a drift report reads as nothing to check rather
    than as an estate holding no grants.
    """
    if (client := decisions.wired(settings, client)) is None:
        return GrantCensusResponse(enabled=False)
    counted = await fga_reads.census(client, exclude=grant_reconcile.platform_objects(settings))
    return GrantCensusResponse(enabled=True, counts_by_type=counted.counts_by_type)


@management_router.post("/ghosts/revoke")
async def revoke_ghost_grants(*, body: GhostRevokeRequest, request: Request, context: AuthzContextDep) -> GhostRevokeResponse:
    """Revoke every grant on each object no registry names, as a drift report found them.

    Each object is judged again on the catalog's own reads (a project or warehouse record, a held location claim, a trash
    record) and one any of them names is refused, not revoked: a trashed table keeps its grants for its undrop, and a
    table a held claim records keeps them while its door may be writing it or while it stands ([[LH-588]]).
    """
    held = await run_in_threadpool(grant_reconcile.held_table_claims, context.settings)
    answer = GhostRevokeResponse()
    for obj in dict.fromkeys(body.objects):
        claim = held.get(obj)
        reason = await _claim_refusal(request, context.settings, claim) if claim is not None else None
        if reason is None:
            reason = await run_in_threadpool(partial(grant_reconcile.ghost_refusal, context.settings, obj))
        if reason is not None:
            answer.refused[obj] = reason
            continue
        answer.revoked[obj] = await object_grants.revoke_ghost_object(context, obj)
    return answer


async def _claim_refusal(request: Request, settings: Settings, claim: location_claims.LocationClaim) -> str | None:
    """:func:`grant_reconcile.claim_refusal` on the store's clock, asking the claim's warehouse whether its table stands.

    Raises:
        ServiceUnavailableError: The backend could not tell whether the table stands.
    """
    store = table_claims.claim_store(settings)
    now = await run_in_threadpool(location_claims.store_now, store, claim.location)
    try:
        ns = await namespace_for_table(request, settings, claim.segments)
    except PermissionDeniedError:
        return "the warehouse of the table its location claim records is deactivated"
    return await run_in_threadpool(partial(grant_reconcile.claim_refusal, claim, now=now, stands=partial(table_claims.holder_stands, ns, store, claim)))


@management_router.post("/tenancy/rebuild")
async def rebuild_warehouse_tenancy(*, body: TenancyRebuildRequest, context: AuthzContextDep) -> TenancyRebuildResponse:
    """Write each warehouse's tenancy edge, ``project:<p> project warehouse:<id>``, as its registry record names it.

    A structural edge confers nothing on its own, so rebuilding it re-grants no one; a warehouse whose record names no
    project, or no record at all, is answered unjustified.
    """
    answer = TenancyRebuildResponse()
    edges: list[fga_writes.ClientTuple] = []
    for warehouse_id in dict.fromkeys(body.warehouses):
        record = await run_in_threadpool(warehouses.get_warehouse, context.settings.registry_root, context.settings.storage_options(), warehouse_id)
        match grant_reconcile.tenancy_edge(record, warehouse_id):
            case str() as reason:
                answer.unjustified[warehouse_id] = reason
            case edge:
                edges.append(edge)
    await object_grants.write_registry_edges(context, edges)
    answer.written = [f"{edge.user} {edge.relation} {edge.object}" for edge in edges]
    return answer
