"""Request/response models for the atlas endpoints (``/api/atlas/*``)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AtlasStatusResponse(BaseModel):
    """Which projection spaces are built + the requested space's projected rows.

    ``spaces`` always reports every DECLARED space's built-ness (so the UI can gate its toggle);
    ``projected``/``rows`` reflect the requested ``space``.

    ``space`` is a plain string — the descriptor names its own projection spaces, so a fixed enum
    here could not describe a corpus that declares any others. This model previously WAS such an
    enum (``text``/``visual``/``caption``), unused by any route and unable to describe half the
    corpora the route serves; that is why the route's answer went out as a bare mapping instead
    (VS-18).
    """

    projected: bool
    rows: int
    space: str
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


class ChunkHits(BaseModel):
    """Rows addressed by key, plus the identity fields that address them.

    ``rows`` are corpus rows: their columns are DERIVED FROM THE DESCRIPTOR (`_hit_columns`), so
    the envelope is the part that can be declared and the row shape stays an explicit
    ``dict[str, Any]`` — the finding's own recommendation for a dataset-dependent body.

    ``key_fields`` rides beside them because the rows come back UNORDERED and possibly short, so a
    caller re-associates by reading each row's own key — which means knowing which columns form it.
    """

    rows: list[dict[str, Any]] = Field(default_factory=list)
    key_fields: list[str] = Field(default_factory=list)
