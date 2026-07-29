"""Transcript endpoints — whole-document transcript + per-chunk word alignments.

Ported from the two transcript endpoints of the pre-split ``backend/search/
router.py``, with the row table, identity keys, time columns, body column, and
alignments column all resolved from the dataset descriptor: rows come from
``declared.search.row_table``, ordering is by ``declared.time.start`` (identity
keys as tiebreaker), the transcript body is ``declared.display.body``, and the
alignments column is the ``alignments`` capability's column part. Sync handlers
→ threadpool (the Lance reads are blocking).
"""

import logging
from typing import Any

from fastapi import APIRouter

from service_kit.exceptions import NotFoundError
from service_kit.lancekit.alignments import parse_alignments_json
from service_kit.lancekit.descriptor import Declared
from service_kit.lancekit.keys import chunk_key_filter, validate_doc_key
from service_kit.lancekit.predicate import eq
from service_kit.lancekit.registry import table_dataset
from service_kit.media.deps import DatasetParam, StateDep
from service_kit.media.state import dataset_handle


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["transcripts"])


def alignments_binding(declared: Declared) -> tuple[str, str] | None:
    """(table, column) of the word-alignments capability, or None when undeclared."""
    target = declared.capabilities.get("alignments")
    if not target:
        return None
    table, _, column = target.partition(".")
    return (table, column) if column else None


@router.get("/doc-transcript/{doc_id}")
def doc_transcript(doc_id: str, state: StateDep, dataset: DatasetParam = None) -> dict[str, Any]:
    """Whole-document, chunk-segmented transcript — ordered by start time. One lazy
    fetch drives both a clickable chunk timeline and (flattened) the player's
    karaoke track. Chunks with no timing keep ``alignments == []`` (media still
    plays, no karaoke). No vector/_score column is projected, so the plain
    dataset scan can't hit the FTS/vector _score restriction.
    """
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    doc_id = validate_doc_key(declared, doc_id)
    if declared.search is None:
        raise NotFoundError("no row table declared for dataset")
    row_table = declared.search.row_table
    info = handle.descriptor.tables.get(row_table)
    if info is None:
        raise NotFoundError("row table missing from dataset")
    present = {c.name for c in info.columns}

    identity = declared.identity
    keys = [f for f in identity.key_fields if f != identity.doc_key]
    time_cols = [declared.time.start, declared.time.end] if declared.time else []
    align = alignments_binding(declared)
    align_col = align[1] if align is not None and align[0] == row_table else None
    wanted = [*keys, *time_cols, declared.display.body, align_col]
    columns = [c for c in dict.fromkeys(wanted) if c is not None and c in present]

    ds = table_dataset(handle, row_table)
    rows = ds.to_table(columns=columns, filter=eq(identity.doc_key, doc_id)).to_pylist()
    # Order by the contract field (declared start time) directly — key fields are
    # source-assigned ids, not sequential indexes, so they are only the stable
    # tiebreaker for chunks that share a start.
    sort_cols = [c for c in [*time_cols[:1], *keys] if c in columns]
    rows.sort(key=lambda r: tuple(r[c] for c in sort_cols))
    for r in rows:
        r["alignments"] = parse_alignments_json(r.pop(align_col, None) if align_col else None)
    return {"doc_id": doc_id, "chunks": rows}


@router.get("/chunk-alignments/{doc_id}/{speech_id}/{chunk_id}")
def chunk_alignments(
    doc_id: str,
    speech_id: int,
    chunk_id: int,
    state: StateDep,
    dataset: DatasetParam = None,
) -> dict[str, Any]:
    """Per-word alignments for one chunk — lazy-fetched by the player when a hit is
    opened. Search results omit the multi-KB alignments blob (it was ~93% of the
    payload and only the selected hit renders it), so the player fetches the real
    array here on demand. The path params map positionally onto the descriptor's
    identity key fields (doc key first); no alignments capability → always ``[]``.
    """
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    doc_id = validate_doc_key(declared, doc_id)
    binding = alignments_binding(declared)
    if binding is None:
        return {"alignments": []}
    table, column = binding
    info = handle.descriptor.tables.get(table)
    if info is None or info.column(column) is None:
        return {"alignments": []}
    rows = table_dataset(handle, table).to_table(columns=[column], filter=chunk_key_filter(declared, doc_id, (speech_id, chunk_id))).to_pylist()
    return {"alignments": parse_alignments_json(rows[0][column]) if rows else []}
