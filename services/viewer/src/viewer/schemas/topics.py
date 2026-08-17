"""Response model for the topics endpoint (``/api/topics``)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class TopicsResponse(BaseModel):
    """The topic hierarchy for the treemap, or ``built: false`` when ungenerated.

    ``hierarchy`` is the opaque nested ``{name, children | value}`` tree decoded
    from Lance JSONB and passed straight to the LayerChart ``<Treemap>``; it is
    deliberately ``Any`` so response serialization never strips a treemap field.
    ``noise_label`` is surfaced so the frontend reads it instead of re-hardcoding
    the unclustered-bucket name.
    """

    built: bool
    layers: int = 0
    n_chunks: int = 0
    hierarchy: Any | None = None
    noise_label: str
