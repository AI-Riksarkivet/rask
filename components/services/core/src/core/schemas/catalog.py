from pydantic import BaseModel

from core.models.enums import BrowseTier


class CatalogRow(BaseModel):
    """Lance columns for `archive_catalog`. Field order = projection order."""

    id: str | None = None
    reference_code: str | None = None
    archive_code: str | None = None
    fonds_id: str | None = None
    fonds_title: str | None = None
    series_id: str | None = None
    series_title: str | None = None
    volume_id: str | None = None
    volume_title: str | None = None
    date_text: str | None = None
    date_start: int | None = None
    date_end: int | None = None
    description: str | None = None
    bild_id: str | None = None
    bildvisning_url: str | None = None
    iiif_manifest: str | None = None
    thumbnail_url: str | None = None


class CatalogHit(CatalogRow):
    listed: bool | None = None
    cached: bool | None = None
    transcribed: bool | None = None


class CatalogSearchResponse(BaseModel):
    ok: bool
    query: str
    count: int
    hits: list[CatalogHit]


class CatalogBrowseResponse(BaseModel):
    ok: bool
    tier: BrowseTier
    count: int
    total: int
    offset: int = 0
    hits: list[CatalogHit]


class CatalogStats(BaseModel):
    available: bool
    rows: int
