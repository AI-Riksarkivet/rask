"""Request/response models for the atlas endpoints (``/api/atlas/*``)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class AtlasSpace(StrEnum):
    """The precomputed projection spaces the Atlas map can render.

    A ``StrEnum`` so FastAPI validates ``?space=`` at the route boundary (422 on
    an unknown value) and the member compares/hashes equal to its string value —
    so it indexes ``SPACES`` (whose keys are the same literals) directly, and
    the ``warmup`` loop that iterates the dict's string keys is unaffected (the
    ``(space, version)`` cache key stays the same hashable tuple either way).
    """

    text = "text"
    visual = "visual"
    caption = "caption"


class AtlasStatusResponse(BaseModel):
    """Which projection spaces are built + the requested space's projected rows.

    ``spaces`` always reports every space's presence (so the UI can gate its
    Text/Visual/Caption toggle); ``projected``/``rows`` reflect ``space``.
    """

    projected: bool
    rows: int
    space: AtlasSpace
    spaces: dict[str, bool]


class ChunkRowIds(BaseModel):
    """A batch of stable Lance row addresses (``_rowid``) for selected points.

    The frontend reads these from /points (one per scatter point) and sends back
    exactly the selected subset — far cheaper than re-deriving rows from keys.
    """

    rowids: list[int]
