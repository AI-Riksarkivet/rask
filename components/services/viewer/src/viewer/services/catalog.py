"""EAD catalog FTS + bulk lookups over the LanceDB table
`s3://<search-bucket>/archive_catalog`.

LanceDB exposes async — no threadpool wrapping needed. Batch-status tiers
come from the ORM via the repository.
"""

import logging
import re

from lancedb.table import AsyncTable
from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.core.exceptions import ValidationError
from viewer.models.batch import BrowseTier
from viewer.repositories import batch as batch_repo
from viewer.schemas.catalog import (
    CatalogBrowseResponse,
    CatalogHit,
    CatalogSearchResponse,
    CatalogStats,
)
from viewer.services.batches import local_batch_status


log = logging.getLogger(__name__)

# `bild_id` (== batch_id) is a Riksarkivet archive code: alphanumerics, dashes,
# underscores, dots. Anything else can't reach LanceDB's filter string — its
# filter language has no parameterized form to fall back on.
_BILD_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_FTS_COLUMN = "search_text"


def _validate_bild_ids(bild_ids: list[str]) -> None:
    for bid in bild_ids:
        if not _BILD_ID_RE.match(bid):
            raise ValidationError(f"invalid bild_id format: {bid!r}")


_COLS = [
    "id",
    "reference_code",
    "archive_code",
    "fonds_id",
    "fonds_title",
    "series_id",
    "series_title",
    "volume_id",
    "volume_title",
    "date_text",
    "date_start",
    "date_end",
    "description",
    "bild_id",
    "bildvisning_url",
    "iiif_manifest",
    "thumbnail_url",
]


async def search_catalog(
    tbl: AsyncTable | None,
    session: AsyncSession,
    query: str,
    limit: int,
) -> CatalogSearchResponse:
    if tbl is None:
        return CatalogSearchResponse(ok=True, query=query, count=0, hits=[])
    fts = await tbl.search(query, query_type="fts", fts_columns=_FTS_COLUMN)
    rows = await fts.select(_COLS).limit(limit).to_list()
    bild_ids = [r["bild_id"] for r in rows if r.get("bild_id")]
    listed, cached, transcribed = await local_batch_status(session, bild_ids)
    hits: list[CatalogHit] = []
    for row in rows:
        bid = row.get("bild_id")
        hit = CatalogHit.model_validate(row)
        hit.listed = bid in listed
        hit.cached = bid in cached
        hit.transcribed = bid in transcribed
        hits.append(hit)
    return CatalogSearchResponse(ok=True, query=query, count=len(hits), hits=hits)


async def catalog_stats(tbl: AsyncTable | None) -> CatalogStats:
    if tbl is None:
        return CatalogStats(available=False, rows=0)
    return CatalogStats(available=True, rows=await tbl.count_rows())


async def by_bild_ids(tbl: AsyncTable | None, bild_ids: list[str]) -> dict[str, CatalogHit]:
    """Bulk-lookup bild_id → CatalogHit for many ids in one LanceDB scan."""
    if not bild_ids or tbl is None:
        return {}
    _validate_bild_ids(bild_ids)
    quoted = ",".join(f"'{bid}'" for bid in bild_ids)
    rows = await tbl.query().where(f"bild_id IN ({quoted})").select(_COLS).to_list()
    return {r["bild_id"]: CatalogHit.model_validate(r) for r in rows}


async def by_bild_id(tbl: AsyncTable | None, bild_id: str) -> CatalogHit | None:
    if not bild_id or tbl is None:
        return None
    _validate_bild_ids([bild_id])
    rows = await tbl.query().where(f"bild_id = '{bild_id}'").select(_COLS).limit(1).to_list()
    return CatalogHit.model_validate(rows[0]) if rows else None


async def browse(
    tbl: AsyncTable | None,
    session: AsyncSession,
    tier: BrowseTier,
    limit: int,
    offset: int,
) -> CatalogBrowseResponse:
    total = await batch_repo.count_at_tier(session, tier)
    rows = await batch_repo.browse_at_tier(session, tier, limit, offset)
    bild_ids = [bid for bid, _, _ in rows]
    cached = {bid for bid, c, _ in rows if c > 0}
    transcribed = {bid for bid, _, t in rows if t > 0}
    catalog_rows = await by_bild_ids(tbl, bild_ids)

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
