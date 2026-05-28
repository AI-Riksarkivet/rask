from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import Response

from viewer.api.dependencies import LinesTblDep, S3Dep, SettingsDep
from viewer.core.exceptions import NotFoundError
from viewer.schemas.search import SearchResponse, SearchStats
from viewer.services.discover import search as search_service


router = APIRouter(prefix="/search", tags=["search"])

_THUMB_CACHE_MAX_AGE_SECS = 24 * 60 * 60  # 24h — line-crop thumbs are immutable per dataset version


@router.get("/")
async def search_lines(
    tbl: LinesTblDep,
    settings: SettingsDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> SearchResponse:
    return await search_service.search_lines(tbl, q, limit, timedelta(seconds=settings.lance_query_timeout_seconds))


@router.get("/stats")
async def search_stats(tbl: LinesTblDep) -> SearchStats:
    return await search_service.stats(tbl)


@router.get("/thumb/{thumb_path:path}")
def search_thumb(thumb_path: str, s3: S3Dep, settings: SettingsDep) -> Response:
    data = search_service.fetch_thumb(s3, settings.search_bucket, thumb_path)
    if data is None:
        raise NotFoundError("thumbnail not found")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": f"public, max-age={_THUMB_CACHE_MAX_AGE_SECS}"})
