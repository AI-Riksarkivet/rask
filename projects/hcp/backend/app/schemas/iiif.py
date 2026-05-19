"""Pydantic models for IIIF endpoints.

Uses Pydantic v2 for request validation and response serialization.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field


# ── Request query models ─────────────────────────────────────────


class IiifBatchParams(BaseModel):
    """Parameters identifying a IIIF batch."""

    batch_id: Annotated[
        str,
        Field(
            min_length=1,
            pattern=r"^[A-Za-z0-9_-]+$",
            description="Volume/batch ID (e.g. C0074667)",
        ),
    ]
    query_params: str = Field(
        "full/max/0/default.jpg",
        description="IIIF image API parameters (region/size/rotation/quality.format)",
    )


# ── Response models ──────────────────────────────────────────────


class IiifManifestResponse(BaseModel):
    """Image IDs extracted from a IIIF manifest."""

    batch_id: str
    image_count: Annotated[int, Field(ge=0)]
    image_ids: list[str]


class IiifImageUrl(BaseModel):
    """A single image ID with its resolved URL."""

    image_id: str
    url: str


class IiifImageUrlsResponse(BaseModel):
    """Resolved image URLs for a batch."""

    batch_id: str
    query_params: str
    extension: str
    image_count: Annotated[int, Field(ge=0)]
    images: list[IiifImageUrl]
