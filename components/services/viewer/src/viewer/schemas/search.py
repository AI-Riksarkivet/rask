from pydantic import BaseModel, field_validator

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

    @field_validator("polygon", mode="before")
    @classmethod
    def _parse_polygon(cls, v: object) -> object:
        """The `lines` index stores polygon as the raw ALTO POINTS string
        ("x,y x,y …"); coerce it to the [[x, y], …] shape the API exposes."""
        if isinstance(v, str):
            if not v.strip():
                return None
            pts: list[list[float]] = []
            for pair in v.split():
                x, _, y = pair.partition(",")
                if y:
                    pts.append([float(x), float(y)])
            return pts or None
        return v


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
