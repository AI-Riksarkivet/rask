from typing import Annotated

from fastapi import APIRouter, Query

from viewer.api.dependencies import CatalogTblDep, SessionDep
from viewer.core.exceptions import NotFoundError
from viewer.models.enums import BrowseTier
from viewer.schemas.catalog import CatalogBrowseResponse, CatalogHit, CatalogSearchResponse, CatalogStats
from viewer.services import catalog as catalog_service


router = APIRouter(tags=["catalog"])


@router.get("/api/catalog/search")
async def catalog_search(
    tbl: CatalogTblDep,
    session: SessionDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> CatalogSearchResponse:
    return await catalog_service.search_catalog(tbl, session, q, limit)


@router.get("/api/catalog/search/stats")
async def catalog_stats(tbl: CatalogTblDep) -> CatalogStats:
    return await catalog_service.catalog_stats(tbl)


@router.get("/api/batches/{batch_id}/catalog")
async def catalog_for_batch(batch_id: str, tbl: CatalogTblDep) -> CatalogHit:
    hit = await catalog_service.by_bild_id(tbl, batch_id)
    if hit is None:
        raise NotFoundError(f"no catalog entry for {batch_id}")
    return hit


@router.get("/api/catalog/browse")
async def catalog_browse(
    tbl: CatalogTblDep,
    session: SessionDep,
    tier: BrowseTier = BrowseTier.CACHED,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CatalogBrowseResponse:
    return await catalog_service.browse(tbl, session, tier, limit, offset)
