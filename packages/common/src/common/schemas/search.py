"""Response models for the search package's document-transcript endpoint."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class DocTranscriptChunk(BaseModel):
    """One chunk of a document transcript, with its per-word ``alignments``."""

    speech_id: int
    chunk_id: int
    start: float
    end: float
    text: str
    alignments: list[dict[str, Any]]


class DocTranscriptResponse(BaseModel):
    """Whole-document transcript, chunk-segmented and ordered by start time."""

    doc_id: str
    chunks: list[DocTranscriptChunk]
