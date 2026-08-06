"""Request/response models for the atlas endpoints (``/api/atlas/*``)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


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


class RowKey(BaseModel):
    """One row addressed the way a TASK carries it: the doc key plus the remaining identity
    fields, positional against ``descriptor.identity.key_fields`` minus the doc key."""

    doc_id: str
    keys: list[int] = Field(default_factory=list)


class ChunkKeys(BaseModel):
    """A batch of rows addressed BY KEY rather than by ``_rowid``.

    ``ChunkRowIds`` above is the cheaper address and stays the right one for anything that came
    from /points — the atlas hands back the very ids it was given. But a labelling TASK does not
    carry a ``_rowid``: it carries the descriptor key-path it was sent with, and row addresses are
    only stable for the table version they were read at, so a task minted last week cannot hold
    one. Joining the corpus row back onto a queue row therefore needs this door (#60).
    """

    keys: list[RowKey] = Field(default_factory=list)
