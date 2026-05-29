"""EAD catalog FTS + bulk lookups over the LanceDB table
`s3://<search-bucket>/archive_catalog`.

LanceDB exposes async — no threadpool wrapping needed. Batch-status tiers
come from the ORM via the repository.
"""

import logging
import re
from datetime import timedelta

from lancedb.expr import col, lit
from lancedb.table import AsyncTable
from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.core.exceptions import ValidationError
from viewer.models.enums import BrowseTier
from viewer.repositories import batch as batch_repo
from viewer.schemas.catalog import (
    CatalogBrowseResponse,
    CatalogHit,
    CatalogRow,
    CatalogSearchResponse,
    CatalogStats,
)
from viewer.services.batches import local_batch_status


log = logging.getLogger(__name__)

# `bild_id` (== batch_id) is a Riksarkivet archive code: alphanumerics, dashes,
# underscores, dots. Anything else can't reach LanceDB's filter string — the IN
# clause has no parameterized form (Expr API has no .in_() yet) so we regex-guard.
_BILD_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_FTS_COLUMN = "search_text"


def _validate_bild_ids(bild_ids: list[str]) -> None:
    for bid in bild_ids:
        if not _BILD_ID_RE.match(bid):
            raise ValidationError(f"invalid bild_id format: {bid!r}")


_CATALOG_COLS = list(CatalogRow.model_fields)


async def search_catalog(
    tbl: AsyncTable | None,
    session: AsyncSession,
    query: str,
    limit: int,
    timeout: timedelta,
) -> CatalogSearchResponse:
    if tbl is None:
        return CatalogSearchResponse(ok=True, query=query, count=0, hits=[])
    fts = tbl.query().nearest_to_text(query, columns=_FTS_COLUMN)
    rows = await fts.select(_CATALOG_COLS).limit(limit).to_list(timeout=timeout)
    bild_ids = [r["bild_id"] for r in rows if r.get("bild_id")]
    status = await local_batch_status(session, bild_ids)
    hits: list[CatalogHit] = []
    for row in rows:
        bid = row.get("bild_id")
        hit = CatalogHit.model_validate(row)
        hit.listed = bid in status.listed
        hit.cached = bid in status.cached
        hit.transcribed = bid in status.transcribed
        hits.append(hit)
    return CatalogSearchResponse(ok=True, query=query, count=len(hits), hits=hits)


async def catalog_stats(tbl: AsyncTable | None) -> CatalogStats:
    if tbl is None:
        return CatalogStats(available=False, rows=0)
    return CatalogStats(available=True, rows=await tbl.count_rows())


async def by_bild_ids(tbl: AsyncTable | None, bild_ids: list[str], timeout: timedelta) -> dict[str, CatalogHit]:
    """Bulk-lookup bild_id → CatalogHit for many ids in one LanceDB scan."""
    if not bild_ids or tbl is None:
        return {}
    _validate_bild_ids(bild_ids)
    quoted = ",".join(f"'{bid}'" for bid in bild_ids)
    rows = await tbl.query().where(f"bild_id IN ({quoted})").select(_CATALOG_COLS).to_list(timeout=timeout)
    return {r["bild_id"]: CatalogHit.model_validate(r) for r in rows}


async def by_bild_id(tbl: AsyncTable | None, bild_id: str, timeout: timedelta) -> CatalogHit | None:
    if not bild_id or tbl is None:
        return None
    rows = await tbl.query().where(col("bild_id").eq(lit(bild_id))).select(_CATALOG_COLS).limit(1).to_list(timeout=timeout)
    return CatalogHit.model_validate(rows[0]) if rows else None


async def browse(
    tbl: AsyncTable | None,
    session: AsyncSession,
    tier: BrowseTier,
    limit: int,
    offset: int,
    timeout: timedelta,
) -> CatalogBrowseResponse:
    total = await batch_repo.count_at_tier(session, tier)
    rows = await batch_repo.browse_at_tier(session, tier, limit, offset)
    bild_ids = [r.batch_id for r in rows]
    cached = {r.batch_id for r in rows if r.cached_pages > 0}
    transcribed = {r.batch_id for r in rows if r.transcribed_pages > 0}
    catalog_rows = await by_bild_ids(tbl, bild_ids, timeout)

    hits: list[CatalogHit] = []
    for bid in bild_ids:
        hit = catalog_rows.get(bid)
        if hit is None:
            continue
        hit.listed = True
        hit.cached = bid in cached
        hit.transcribed = bid in transcribed
        hits.append(hit)
    return CatalogBrowseResponse(ok=True, tier=tier, count=len(hits), total=total, offset=offset, hits=hits)
