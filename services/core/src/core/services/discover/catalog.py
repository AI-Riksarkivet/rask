"""EAD catalog FTS + stats over the LanceDB table `s3://<search-bucket>/archive_catalog`.

LanceDB exposes async — no threadpool wrapping needed. The batches.db status tiers (listed / cached /
transcribed) died with the batches table (P7a): hits are pure EAD rows now. Transitional — retires with
the R6/R20 media wave (a catalog-governed EAD table served by the media estate).
"""

import logging
from datetime import timedelta

from lancedb.table import AsyncTable

from core.schemas.catalog import (
    CatalogHit,
    CatalogRow,
    CatalogSearchResponse,
    CatalogStats,
)


log = logging.getLogger(__name__)

_FTS_COLUMN = "search_text"

_CATALOG_COLS = list(CatalogRow.model_fields)


async def search_catalog(
    tbl: AsyncTable | None,
    query: str,
    limit: int,
    timeout: timedelta,
) -> CatalogSearchResponse:
    if tbl is None:
        return CatalogSearchResponse(ok=True, query=query, count=0, hits=[])
    fts = tbl.query().nearest_to_text(query, columns=_FTS_COLUMN)
    rows = await fts.select(_CATALOG_COLS).limit(limit).to_list(timeout=timeout)
    hits = [CatalogHit.model_validate(row) for row in rows]
    return CatalogSearchResponse(ok=True, query=query, count=len(hits), hits=hits)


async def catalog_stats(tbl: AsyncTable | None) -> CatalogStats:
    if tbl is None:
        return CatalogStats(available=False, rows=0)
    return CatalogStats(available=True, rows=await tbl.count_rows())
