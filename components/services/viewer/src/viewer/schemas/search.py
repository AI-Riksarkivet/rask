from pydantic import BaseModel

from viewer.schemas.catalog import CatalogHit


class LineRow(BaseModel):
    """Lance columns for `lines`. Field order = projection order."""

    batch_id: str
    page_id: str | None = None
    page_idx: int | None = None
    line_id: str | None = None
    line_idx: int | None = None
    text: str
    confidence: float | None = None
    hpos: float | None = None
    vpos: float | None = None
    width: float | None = None
    height: float | None = None
    polygon: list[list[float]] | None = None
    thumb_key: str | None = None


class SearchHit(LineRow):
    thumb_url: str | None = None
    catalog: CatalogHit | None = None


class SearchResponse(BaseModel):
    ok: bool
    query: str
    count: int
    hits: list[SearchHit]


class SearchStats(BaseModel):
    available: bool
    rows: int
