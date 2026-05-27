from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import Response

from viewer.api.dependencies import LinesTblDep, S3Dep, SettingsDep
from viewer.core.exceptions import NotFoundError
from viewer.schemas.search import SearchResponse, SearchStats
from viewer.services import search as search_service


router = APIRouter(tags=["search"])


@router.get("/api/search")
async def search_lines(
    tbl: LinesTblDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> SearchResponse:
    return await search_service.search_lines(tbl, q, limit)


@router.get("/api/search/stats")
async def search_stats(tbl: LinesTblDep) -> SearchStats:
    return await search_service.stats(tbl)


@router.get("/api/search/thumb/{thumb_path:path}")
def search_thumb(thumb_path: str, s3: S3Dep, settings: SettingsDep) -> Response:
    data = search_service.fetch_thumb(s3, settings.search_bucket, thumb_path)
    if data is None:
        raise NotFoundError("thumbnail not found")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
