from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from core.api.dependencies import CatalogTblDep, SettingsDep
from core.schemas.catalog import CatalogSearchResponse, CatalogStats
from core.services.discover import catalog as catalog_service


router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/search")
async def catalog_search(
    tbl: CatalogTblDep,
    settings: SettingsDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> CatalogSearchResponse:
    return await catalog_service.search_catalog(tbl, q, limit, timedelta(seconds=settings.lance_query_timeout_seconds))


@router.get("/search/stats")
async def catalog_stats(tbl: CatalogTblDep) -> CatalogStats:
    return await catalog_service.catalog_stats(tbl)
