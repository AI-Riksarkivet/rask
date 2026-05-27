"""Line-level FTS over the LanceDB table at `s3://<search-bucket>/lines`.

The table handle comes from `LinesTblDep` (opened once in lifespan); this
module is stateless and async-native (LanceDB exposes an async API).
"""

import logging
from typing import TYPE_CHECKING

from lancedb.table import AsyncTable

from viewer.schemas.search import SearchHit, SearchResponse, SearchStats


if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


log = logging.getLogger(__name__)


_FTS_COLUMN = "text"
_THUMB_KEY_PREFIX = "thumbs/"

_LINE_COLS = [
    "batch_id",
    "page_id",
    "page_idx",
    "line_id",
    "line_idx",
    "text",
    "confidence",
    "hpos",
    "vpos",
    "width",
    "height",
    "polygon",
    "thumb_key",
]


async def search_lines(tbl: AsyncTable | None, query: str, limit: int) -> SearchResponse:
    if tbl is None:
        return SearchResponse(ok=True, query=query, count=0, hits=[])
    fts = await tbl.search(query, query_type="fts", fts_columns=_FTS_COLUMN)
    rows = await fts.select(_LINE_COLS).limit(limit).to_list()
    hits: list[SearchHit] = []
    for row in rows:
        if row.get("thumb_key"):
            row["thumb_url"] = f"/api/search/thumb/{row['thumb_key']}"
        hits.append(SearchHit.model_validate(row))
    return SearchResponse(ok=True, query=query, count=len(hits), hits=hits)


async def stats(tbl: AsyncTable | None) -> SearchStats:
    if tbl is None:
        return SearchStats(available=False, rows=0)
    return SearchStats(available=True, rows=await tbl.count_rows())


def fetch_thumb(s3: "S3Client", bucket: str, thumb_key: str) -> bytes | None:
    """GET a line thumbnail from the search bucket. Returns bytes or None on miss.

    `thumb_key` MUST start with `thumbs/` — defense in depth so the proxy
    can't be tricked into fetching arbitrary keys.
    """
    if not thumb_key.startswith(_THUMB_KEY_PREFIX):
        return None
    try:
        resp = s3.get_object(Bucket=bucket, Key=thumb_key)
        return resp["Body"].read()
    except Exception as exc:
        log.warning("thumb GET %s/%s failed: %s", bucket, thumb_key, exc)
        return None
