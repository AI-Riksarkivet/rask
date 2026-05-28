from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from viewer.api.dependencies import CatalogTblDep, SessionDep, SettingsDep
from viewer.models.enums import BrowseTier
from viewer.schemas.catalog import CatalogBrowseResponse, CatalogSearchResponse, CatalogStats
from viewer.services.discover import catalog as catalog_service


router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/search")
async def catalog_search(
    tbl: CatalogTblDep,
    session: SessionDep,
    settings: SettingsDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> CatalogSearchResponse:
    return await catalog_service.search_catalog(tbl, session, q, limit, timedelta(seconds=settings.lance_query_timeout_seconds))


@router.get("/search/stats")
async def catalog_stats(tbl: CatalogTblDep) -> CatalogStats:
    return await catalog_service.catalog_stats(tbl)


@router.get("/browse")
async def catalog_browse(
    tbl: CatalogTblDep,
    session: SessionDep,
    settings: SettingsDep,
    tier: BrowseTier = BrowseTier.CACHED,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CatalogBrowseResponse:
    return await catalog_service.browse(tbl, session, tier, limit, offset, timedelta(seconds=settings.lance_query_timeout_seconds))
