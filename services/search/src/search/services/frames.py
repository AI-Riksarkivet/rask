"""Frame → row-table join family: vector + FTS search over a frame table.

Ported from ``backend.search.frames``, keyed on the descriptor's identity
fields: ranks the frame table (by an image/caption vector, or BM25 over the
caption text), dedups to one best frame per row key, then fetches the matching
row-table payload rows and re-orders them to the frame ranking. Backs visual /
scene / scene_fts search and the frame legs of ``all``. A join failure raises a
domain :class:`ValidationError` (real error logged, never interpolated), and a ranker that cannot run
raises :class:`ServiceUnavailableError` rather than degrading to ``[]`` — see
:func:`_ranked_or_fallback`. Genuine ABSENCE (no frames extracted, no caption column) is answered
``[]`` by each function's own guard, before any ranking is attempted.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from lancedb.query import MatchQuery

from search.services.constants import (
    VECTOR_MAX_NPROBES,
    VECTOR_NPROBES,
    VECTOR_REFINE_FACTOR,
)
from search.services.postprocess import RowKey, row_key
from service_kit.exceptions import ServiceUnavailableError, ValidationError
from service_kit.lancekit.predicate import eq


#: Cap on OR-of-ANDs composite-key clauses in the frame→row join — Lance's
#: planner errors well past this (the known ~500-800 key-OR ceiling).
_MAX_JOIN_KEYS = 300

if TYPE_CHECKING:
    from collections.abc import Callable

    from search.services.target import SearchTarget

logger = logging.getLogger(__name__)


def _ranked_or_fallback(rank: Callable[..., list[dict[str, Any]]], *, scoped: bool) -> list[dict[str, Any]]:
    """Run ``rank(scoped=True)``; if the scope prefilter references a column the
    frame table lacks (a metadata filter, which stays on the row-table join),
    retry unscoped. A failing UNSCOPED rank is an OUTAGE and is raised.

    IT USED TO RETURN ``[]`` THERE, defended as "no vector/FTS index yet" — and that justification is
    empirically false. Measured against lancedb at HEAD: an FTS query with no FTS index does NOT
    raise (flat scan), and a vector search with no ANN index does NOT raise (flat search, rows
    returned). Absence never reaches this function at all — the callers' guard
    (``vec is None or frame_tbl is None or column not in frame_tbl.schema.names``) has already
    answered ``[]`` for it.

    So the only thing that ``except`` ever caught was a genuine failure — a store outage, a timeout,
    a malformed query — and every one of them was rendered to the caller as "no hits". An empty 200
    says "this corpus has nothing to show you", which is a different claim from "search is down", and
    the search plane is half of what this estate is for.

    The scoped→unscoped retry stays, because THAT case is real and verified: a prefilter naming a
    non-frame column raises ``Invalid user input: Schema error: No field named ...``, and retrying
    unscoped is the documented recovery.
    """
    if scoped:
        try:
            return rank(scoped=True)
        except Exception:
            logger.info("scoped rank failed; retrying unscoped (the prefilter names a non-frame column)", exc_info=True)
    try:
        return rank(scoped=False)
    except Exception as exc:
        raise ServiceUnavailableError(f"the frame ranker is unavailable, so no ranking could be produced: {exc}") from exc


def frame_search(
    frame_tbl: Any,
    target: SearchTarget,
    vec: Any,
    *,
    column: str,
    n: int,
    where: str | None,
    scope_where: str | None = None,
) -> list[dict[str, Any]]:
    """Rank the frame table by a vector ``column``, then fetch the matching
    row-table rows (where the hit payload lives) and re-order to match.

    Backs both visual search (image similarity) and scene search (the caption's
    text vector); the join back to the row table is identical, only the ranked
    column differs.

    ``scope_where`` is an upstream result scope (row keys) pushed down as a
    *prefilter* so a refine ranks WITHIN the scope instead of post-filtering the
    global top-n (which returned ~0). It only references columns the frame
    table has; a non-frame predicate (e.g. a metadata filter, which stays on
    the row-table join via ``where``) falls back to an unscoped rank.

    Returns ``[]`` when frames haven't been extracted/embedded yet — the frame
    pipeline is optional, so visual/scene/all search degrades to empty rather
    than erroring.
    """
    if vec is None or frame_tbl is None or column not in frame_tbl.schema.names:
        return []

    def rank(*, scoped: bool) -> list[dict[str, Any]]:
        qb = (
            frame_tbl.search(vec.tolist(), vector_column_name=column)
            .distance_type("cosine")
            .minimum_nprobes(VECTOR_NPROBES)
            .maximum_nprobes(VECTOR_MAX_NPROBES)
            .refine_factor(VECTOR_REFINE_FACTOR)
            .select([*target.key_fields, "_distance"])
            .limit(n)
        )
        if scoped and scope_where:
            qb = qb.where(scope_where, prefilter=True)
        return qb.to_list()

    ranked = _ranked_or_fallback(rank, scoped=bool(scope_where))
    return frames_to_row_hits(target, ranked, where)


def frame_fts_search(
    frame_tbl: Any,
    target: SearchTarget,
    query: str,
    *,
    column: str,
    n: int,
    where: str | None,
    scope_where: str | None = None,
) -> list[dict[str, Any]]:
    """BM25 keyword search over the frame table's caption column, joined back.

    The keyword counterpart to scene-vector search: same frame→row join as
    :func:`frame_search`, only the ranking is BM25 instead of cosine.
    ``scope_where`` prefilters to an upstream scope (see :func:`frame_search`).
    Returns ``[]`` when the corpus declares no caption column — the guard below, not an ``except``.
    A MISSING FTS INDEX IS NOT THAT CASE: measured against lancedb, an FTS query with no index does
    not raise, it flat-scans. This docstring used to claim the index case was handled by degrading to
    ``[]``, which is how a real ranker failure came to look like "no hits".
    """
    if not query or frame_tbl is None or column not in frame_tbl.schema.names:
        return []

    def rank(*, scoped: bool) -> list[dict[str, Any]]:
        qb = frame_tbl.search(MatchQuery(query, column), query_type="fts").select([*target.key_fields, "_score"]).limit(n)
        if scoped and scope_where:
            qb = qb.where(scope_where, prefilter=True)
        return qb.to_list()

    ranked = _ranked_or_fallback(rank, scoped=bool(scope_where))
    return frames_to_row_hits(target, ranked, where)


def frames_to_row_hits(target: SearchTarget, ranked: list[dict[str, Any]], where: str | None) -> list[dict[str, Any]]:
    """Dedup ranked frame rows to one best frame per row key (ranking is
    best-first, so the first occurrence wins), then fetch the matching row-table
    payload rows in one scan and re-order them to the frame ranking.

    Shared by vector (visual/scene) and FTS (scene keyword) frame search — only
    the upstream ranking differs, the join is identical.
    """
    keys: list[RowKey] = []
    seen: set[RowKey] = set()
    # Carry each row's frame ranking signal (`_distance` for vector search,
    # `_score` for caption FTS) from the best (first) ranked frame, so the hit
    # keeps a score after the join below — the results table / `relevanceOf`
    # reads it. Without this, scene/visual hits arrive scoreless.
    score_by_key: dict[RowKey, dict[str, float]] = {}
    for r in ranked:
        key = row_key(r, target.key_fields)
        if key not in seen:
            seen.add(key)
            keys.append(key)
            score_by_key[key] = {k: r[k] for k in ("_distance", "_score") if k in r}
    if not keys:
        return []
    # The composite-key OR join builds one clause per distinct key; Lance's
    # planner degrades / errors on very large OR-of-ANDs filters (crashes past
    # ~500-800 clauses — the known key-OR ceiling). Cap the join to the
    # best-ranked keys so a legal high-n mode=all search returns its top hits
    # instead of 400ing.
    if len(keys) > _MAX_JOIN_KEYS:
        keys = keys[:_MAX_JOIN_KEYS]
    # Composite-key OR join back to the row table. A pure metadata filter (no
    # vector/FTS query) must go through the Lance dataset's filter scan — a
    # no-query `table.search().where(...)` returns nothing.
    key_filter = " OR ".join("(" + " AND ".join(eq(field, value) for field, value in zip(target.key_fields, key, strict=True)) + ")" for key in keys)
    full_filter = f"({key_filter})" + (f" AND ({where})" if where else "")
    try:
        rows = target.row_ds.to_table(columns=target.payload_columns, filter=full_filter).to_pylist()
    except Exception as e:
        logger.warning("frame search join failed", exc_info=True)
        raise ValidationError("frame search join failed") from e
    by_key = {row_key(r, target.key_fields): r for r in rows}
    return [{**by_key[k], **score_by_key[k]} for k in keys if k in by_key]
