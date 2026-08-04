"""Finalize raw Lance rows into the uniform API hit shape.

Ported from ``backend.search.postprocess``: pops each hit's alignments column
into ``alignments`` (parsed), attaches the representative-frame caption, and
provides the RRF fusion for the multi-ranking modes — all keyed on the
descriptor's identity fields instead of a hardcoded chunk key.
``parse_alignments_json`` is vendored from ``packages/ratch/retrieval/search.py``
because the backend may not import the pipeline package (§4.4).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from search.services.constants import REPRESENTATIVE_FRAME_INDEX
from service_kit.lancekit.predicate import and_, eq, isin


if TYPE_CHECKING:
    from collections.abc import Sequence

    from search.services.target import SearchTarget

#: Stable identity of a row/frame hit: its key-field values, in declared order.
RowKey = tuple[Any, ...]


def parse_alignments_json(raw: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Decode a JSONB alignments column value to a Python list.

    Returns an empty list on null, missing, malformed, or non-list input — so
    the annotated return type holds for every branch (Lance may hand back either
    the raw JSON string or an already-decoded value). Vendored verbatim from
    ``ratch.retrieval.search.parse_alignments_json``.
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


def row_key(hit: dict[str, Any], key_fields: Sequence[str]) -> RowKey:
    """The identity tuple shared by the row table and any frame table."""
    return tuple(hit.get(f) for f in key_fields)


def postprocess_hits(raw: list[dict[str, Any]], target: SearchTarget) -> list[dict[str, Any]]:
    """Finalize hits for the API: parse alignments + attach each frame's caption.

    Every hit carries ``alignments`` (``[]`` when the column isn't projected or
    the capability is undeclared — the uniform shape the frontend renders), and
    ``caption`` whenever the dataset declares a caption source.
    """
    for h in raw:
        value = h.pop(target.alignments_column, None) if target.alignments_column else None
        h["alignments"] = parse_alignments_json(value)
        # PROVENANCE, stamped in the one funnel every search path returns through. Doing it per
        # search path instead is how one of them (the filter-only browse, say) would quietly ship
        # rows that cannot say where they came from — and nothing would fail until two corpora
        # shared a result list.
        h["_dataset"] = target.dataset_id
        h["_table"] = target.table_name
    if target.caption_column:
        attach_captions(
            target.caption_table(),
            raw,
            caption_column=target.caption_column,
            key_fields=target.key_fields,
        )
    return raw


def attach_captions(
    frame_tbl: Any,
    hits: list[dict[str, Any]],
    *,
    caption_column: str,
    key_fields: Sequence[str],
) -> None:
    """Set ``hit['caption']`` from each row's representative frame (frame_idx=0).

    Captions live on the frame table, not the row table, so this one filtered
    scan is how the list/table views learn the scene description for every
    mode. A no-op (leaves no ``caption`` key) when the frame table or the
    caption column is absent — captions are a nice-to-have, never a reason to
    fail a search.
    """
    if not hits or frame_tbl is None or caption_column not in frame_tbl.schema.names:
        return
    keys = {row_key(h, key_fields) for h in hits}
    # One `<doc> IN (...)` scan over the coarse first key field, not a per-hit
    # OR-of-ANDs: DataFusion evaluates a ~100-branch OR predicate row-by-row over
    # every frame (~180ms for a diverse search), whereas IN is a hash-membership
    # scan (~75ms, 2.4x faster, verified identical output). We over-fetch the
    # matched docs' frame-0 rows and pick out the exact keys in Python below.
    doc_field = key_fields[0]
    key_filter = and_(isin(doc_field, (k[0] for k in keys)), eq("frame_idx", REPRESENTATIVE_FRAME_INDEX))
    try:
        rows = frame_tbl.to_lance().to_table(columns=[*key_fields, caption_column], filter=key_filter).to_pylist()
    except Exception:
        return
    by_key = {row_key(r, key_fields): r.get(caption_column) for r in rows}
    for h in hits:
        h["caption"] = by_key.get(row_key(h, key_fields))


def rrf_fuse(rankings: list[list[dict[str, Any]]], *, key_fields: Sequence[str], k: int = 60) -> list[dict[str, Any]]:
    """Reciprocal-rank fusion across N ranked lists keyed on the identity fields.

    Lance's hybrid query handles RRF natively when both FTS and vector are in
    play; this helper covers the multi-leg ``all`` mode where we issue distinct
    queries (text / frame / caption vectors) and must merge them ourselves.
    """
    scored: dict[RowKey, float] = {}
    rep: dict[RowKey, dict[str, Any]] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            key = row_key(hit, key_fields)
            scored[key] = scored.get(key, 0.0) + 1.0 / (k + rank)
            # Keep the first occurrence (highest-ranked) as the canonical row.
            rep.setdefault(key, hit)
    return sorted(rep.values(), key=lambda h: -scored[row_key(h, key_fields)])
