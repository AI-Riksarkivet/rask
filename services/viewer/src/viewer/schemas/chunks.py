"""Response models for the document-chunk endpoints (``/api/doc-chunks``, ``/api/chunk-alignments``).

The ENVELOPE is fixed and belongs in the schema; the rows inside it are not. A chunk row's columns
are derived from the dataset descriptor (identity keys, the declared time axis, the display body),
so they stay an explicit ``dict[str, Any]`` — which is the finding's own line (VS-18): declare what
is structural, and say so where the body is genuinely dataset-dependent, rather than annotating the
whole answer as a mapping and documenting nothing.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DocChunks(BaseModel):
    """One document's chunks, ordered by the declared start time.

    Every row carries an ``alignments`` list (empty when the corpus declares no alignments
    capability or the chunk has no timing) — uniformly present, so the player never branches on key
    existence.
    """

    doc_id: str
    chunks: list[dict[str, Any]] = Field(default_factory=list)


class ChunkAlignments(BaseModel):
    """Per-token timings for one chunk — lazily fetched when a hit is opened.

    Always a list: a corpus with no alignments capability answers ``[]`` rather than 404, because
    "this corpus has no word timings" is not a missing resource. The token objects' own shape is the
    writer's (`parse_alignments_json` decodes whatever the column holds), so they stay untyped here
    rather than being described by a model that would be a guess.
    """

    alignments: list[dict[str, Any]] = Field(default_factory=list)
