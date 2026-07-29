"""Full-text search over the ingested transcript table.

Uses LanceDB's built-in Tantivy FTS index (BM25 ranking). The index is created
by :func:`ratch.ingest.ingest.ingest_many` (and its single-doc wrapper
:func:`ratch.ingest.ingest.ingest_document`) at ingest time, or rebuilt
standalone via :func:`ratch.ingest.ingest.reindex_fts`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import lancedb
from pydantic import BaseModel


# Tantivy boolean/proximity keywords we strip when extracting match terms.
_QUERY_STOPWORDS: frozenset[str] = frozenset({"and", "or", "not", "near"})


class WordMatch(BaseModel):
    """One word-level alignment hit (the fields stored in the ``words`` struct).

    ``score`` is optional — WhisperX leaves it null for some aligned words.
    """

    text: str
    start: float
    end: float
    score: float | None = None


def parse_alignments_json(raw: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Decode the ``alignments_json`` JSONB column to a Python list.

    Returns an empty list on null, missing, malformed, or non-list input — so
    the annotated return type holds for every branch (Lance may hand back either
    the raw JSON string or an already-decoded value).
    """
    if not raw:
        return []
    decoded = raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return decoded if isinstance(decoded, list) else []


def extract_query_terms(query: str) -> list[str]:
    """Return the lowercased content words from a Tantivy-style query.

    Strips operators (``AND``/``OR``/``NOT``/``NEAR``), quotes, parens, and
    punctuation. Used to match individual words inside chunk alignments.
    """
    tokens = re.findall(r"\w+", query.lower(), flags=re.UNICODE)
    return [t for t in tokens if t not in _QUERY_STOPWORDS]


def iter_matching_words(chunk_row: dict[str, Any], terms: list[str]) -> list[WordMatch]:
    """Walk a chunk row's word-level alignments and return words matching any term.

    Parses the ``alignments_json`` column (a JSON string). Comparison is
    case-insensitive and ignores leading/trailing punctuation. Each hit is a
    :class:`WordMatch` (``text`` / ``start`` / ``end`` / optional ``score``).
    """
    if not terms:
        return []
    alignments = parse_alignments_json(chunk_row.get("alignments_json"))
    wanted = set(terms)
    hits: list[WordMatch] = []
    for alignment in alignments or []:
        for word in alignment.get("words") or []:
            clean = re.sub(r"^\W+|\W+$", "", (word.get("text") or "").lower(), flags=re.UNICODE)
            if clean in wanted:
                hits.append(WordMatch.model_validate(word))
    return hits


def _select_columns() -> list[str]:
    """Columns returned by default — omits large nested `alignments` unless
    the caller asks for them."""
    return [
        "doc_id",
        "audio_path",
        "speech_id",
        "chunk_id",
        "start",
        "end",
        "duration",
        "text",
        "language",
    ]


def nearest_chunks(
    db_path: str | Path,
    query: str,
    *,
    table_name: str = "chunks",
    limit: int = 10,
    include_alignments: bool = False,
    where: str | None = None,
) -> list[dict[str, Any]]:
    """BM25 full-text search. Returns a list of row dicts ordered by relevance.

    Args:
        db_path: Filesystem path to the Lance database directory.
        query: Free-form query string. Phrase queries with quotes work, as
            does Tantivy's boolean syntax (``+must -mustnot "phrase"``).
        table_name: Name of the chunk-centric table. Defaults to ``"chunks"``.
        limit: Max number of hits.
        include_alignments: If ``True``, include the nested per-word alignments
            on each row.
        where: Optional SQL filter expression evaluated after the FTS ranking,
            e.g. ``"language = 'en'"`` or ``"audio_path LIKE '%dickens%'"``.
    """
    db = lancedb.connect(str(db_path))
    table = db.open_table(table_name)

    columns = _select_columns()
    if include_alignments:
        columns.append("alignments_json")

    # Request `_score` explicitly: Lance currently auto-adds it to FTS selects
    # but warns that a future version won't. Asking for it keeps the BM25 score
    # available either way and silences the deprecation warning.
    q = table.search(query, query_type="fts").select([*columns, "_score"]).limit(limit)
    if where:
        q = q.where(where, prefilter=False)
    return q.to_list()


def timecode(seconds: float, *, millis: bool = False) -> str:
    """Format a float-seconds timestamp as ``MM:SS`` / ``HH:MM:SS``.

    If ``millis=True``, includes milliseconds: ``MM:SS.mmm`` / ``HH:MM:SS.mmm``.
    """
    total = int(seconds) if millis else round(seconds)
    s = total % 60
    m = (total // 60) % 60
    h = total // 3600
    core = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    if not millis:
        return core
    ms = round((seconds - int(seconds)) * 1000)
    return f"{core}.{ms:03d}"
