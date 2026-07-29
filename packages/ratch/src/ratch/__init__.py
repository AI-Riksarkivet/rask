"""Lance table for easytranscriber AudioMetadata JSON — FTS-ready."""

from .ingest.ingest import flatten_chunks, ingest_document, ingest_many, load_transcript
from .model.datamodel import (
    AlignmentSegment,
    AudioChunk,
    AudioMetadata,
    SpeechSegment,
    WordSegment,
)
from .model.schema import (
    CHUNK_SCHEMA,
    DOC_SCHEMA,
    DOC_STORAGE_VERSION,
    alignment_struct,
    word_struct,
)
from .retrieval.search import nearest_chunks, timecode


__all__ = [  # noqa: RUF022 — grouped by layer (more readable than alphabetical)
    # Data model (mirrors easytranscriber)
    "AlignmentSegment",
    "AudioChunk",
    "AudioMetadata",
    "SpeechSegment",
    "WordSegment",
    # Schema
    "CHUNK_SCHEMA",
    "DOC_SCHEMA",
    "DOC_STORAGE_VERSION",
    "alignment_struct",
    "word_struct",
    # Ingest
    "flatten_chunks",
    "ingest_document",
    "ingest_many",
    "load_transcript",
    # Search
    "nearest_chunks",
    "timecode",
]
