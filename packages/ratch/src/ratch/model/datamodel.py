"""Pydantic v2 data model mirroring ``easytranscriber.data.datamodel``.

Vendored so the ingest path can parse transcriber output type-safely
(:meth:`AudioMetadata.model_validate_json`) without pulling in easytranscriber's
heavy runtime deps (torch, transformers, pyannote, ...). Field names and shapes
match the upstream model; ``extra="ignore"`` tolerates transcriber fields we
don't model. If ``easytranscriber`` is installed you can swap to its structs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    # Transcriber JSON carries fields we don't model; ignore them rather than
    # error (mirrors msgspec's tolerant decode that this model replaced).
    model_config = ConfigDict(extra="ignore")


class WordSegment(_Base):
    """One word-level alignment timing; ``score`` is null for some aligned words."""

    text: str
    start: float
    end: float
    score: float | None = None


class AudioChunk(_Base):
    """One decoded chunk within a speech segment: text, timing + decode stats."""

    start: float
    end: float
    text: str | None = None
    duration: float | None = None
    audio_frames: int | None = None
    num_logits: int | None = None
    language: str | None = None
    language_prob: float | None = None
    id: str | int | None = None


class AlignmentSegment(_Base):
    """One aligned text span with its per-word ``words`` timings."""

    start: float
    end: float
    text: str
    words: list[WordSegment] = Field(default_factory=list)
    id: str | int | None = None
    duration: float | None = None
    score: float | None = None


class SpeechSegment(_Base):
    """One detected speech region: its decoded ``chunks`` + word ``alignments``."""

    speech_id: str | int | None = None
    start: float | None = None
    end: float | None = None
    text: str | None = None
    text_spans: list[tuple[int, int]] | None = None
    chunks: list[AudioChunk] = Field(default_factory=list)
    alignments: list[AlignmentSegment] = Field(default_factory=list)
    duration: float | None = None
    audio_frames: int | None = None
    probs_path: str | None = None
    metadata: dict[str, Any] | None = None


class AudioMetadata(_Base):
    """Top-level transcriber output for one media file (path, duration, speeches)."""

    audio_path: str
    sample_rate: int
    duration: float
    id: str | int | None = None
    speeches: list[SpeechSegment] | None = None
    metadata: dict[str, Any] | None = None
