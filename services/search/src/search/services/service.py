"""Decoupled search business logic — the mode-aware retrieval core.

Ported from ``backend.search.service``, descriptor-driven: ``run_search`` takes
a :class:`~common.lancekit.registry.DatasetHandle`, resolves it to a
:class:`~search.services.target.SearchTarget` once, and routes a
:class:`~search.services.spec.SearchSpec` across the seven modes
(fts / semantic / visual / scene / scene_fts / hybrid / all), returning a
uniform hit shape. Each mode is one handler in :data:`_MODE_HANDLERS` (dict
dispatch — adding a mode = adding an entry, not editing a chain); handlers
share a :class:`SearchContext` and own their rerank policy, while
``run_search`` owns the WHERE composition, the embedding step, and the uniform
postprocess tail.

Mode availability follows the descriptor: modes whose vector binding is
undeclared degrade to ``[]`` (visual/scene/scene_fts — optional enrichments)
or 400 (semantic/hybrid — the caller asked for a space that doesn't exist).
Vector legs whose binding lives on the row table search it directly; legs on a
frame table rank there and join back by the identity key fields.

It takes the two vLLM client getters as plain callables, so this module never
imports the FastAPI app or app state — only domain exceptions from
:mod:`common.core.exceptions` for error mapping. The HTTP router wires it to
the request via dependency injection.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
from common.core.exceptions import ServiceUnavailableError, ValidationError
from pydantic import BaseModel, ConfigDict

from search.services.constants import (
    VECTOR_MAX_NPROBES,
    VECTOR_NPROBES,
    VECTOR_REFINE_FACTOR,
)
from search.services.filters import build_where_clause
from search.services.frames import frame_fts_search, frame_search
from search.services.postprocess import postprocess_hits, rrf_fuse
from search.services.rerank import rerank_by_text
from search.services.spec import SearchMode, SearchSpec
from search.services.target import SearchTarget, resolve_target
from search.services.vector import vector_search


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from common.lancekit.descriptor import FtsBinding, VectorBinding
    from common.lancekit.registry import DatasetHandle

    from search.services.encoders.embedding import EmbeddingClient
    from search.services.encoders.reranker import VLLMReranker

logger = logging.getLogger(__name__)

__all__ = ["run_search"]


class SearchContext(BaseModel):
    """Everything a mode handler needs, resolved once by ``run_search``.

    The Lance handles (inside ``target``) aren't Pydantic-validatable, hence
    ``arbitrary_types_allowed``. ``text_vec``/``image_vec`` stay ``None`` for
    the embedding-free modes (fts / scene_fts).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    target: SearchTarget
    get_reranker: Any  # Callable[[], VLLMReranker]
    spec: SearchSpec
    where: str | None
    # Text fed to the cross-encoder reranker = the user's full text intent
    # (keyword + meaning question). The reranker is text-only.
    rerank_query: str
    text_vec: Any | None = None
    image_vec: Any | None = None


def _maybe_rerank(ctx: SearchContext, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the optional cross-encoder head rerank per the spec toggle.

    The client getter maps *connection* failures to 503; a failure during the
    rerank call itself (server restarting, mid-request drop) would otherwise
    surface as a raw 500, so it gets the same mapping here.
    """
    if not ctx.spec.rerank:
        return hits
    try:
        return rerank_by_text(
            ctx.get_reranker,
            ctx.rerank_query,
            hits,
            body_column=ctx.target.body_column,
            rerank_n=ctx.spec.rerank_n,
            n=ctx.spec.n,
        )
    except httpx.HTTPError as e:
        logger.warning("rerank call failed", exc_info=True)
        raise ServiceUnavailableError("rerank service unavailable") from e


def _fts_binding(target: SearchTarget) -> FtsBinding:
    """The FTS binding, required to live on the row table (hits ARE row rows)."""
    fts = target.fts
    if fts is None or fts.table != target.row_table_name:
        raise ValidationError("keyword search is not configured for this dataset")
    return fts


def _query_vec(ctx: SearchContext, binding: VectorBinding) -> Any | None:
    """Pick the query vector a binding can be searched with, or ``None`` if the
    search box can't drive its modality.

    The bi-encoder maps text and images into one space, so a text query can rank
    an image column (and vice versa) when only one vector exists. A space whose
    query encoder is neither ``text`` nor ``image`` (e.g. an audio voiceprint)
    has no query vector the search box can produce — it is served by its own
    capability, not generic search — so return ``None`` and let the caller skip
    that leg rather than feed it a wrong-modality / wrong-dimension vector.
    """
    if binding.query_encoder == "image":
        return ctx.image_vec if ctx.image_vec is not None else ctx.text_vec
    if binding.query_encoder == "text":
        return ctx.text_vec if ctx.text_vec is not None else ctx.image_vec
    return None


def _vector_leg(ctx: SearchContext, binding: VectorBinding, vec: Any, *, n: int) -> list[dict[str, Any]]:
    """One ranked vector leg: direct on the row table, or frame-ranked + joined."""
    target = ctx.target
    if binding.table == target.row_table_name:
        return vector_search(
            target.row_tbl,
            vec,
            binding.column,
            payload_columns=target.payload_columns,
            n=n,
            where=ctx.where,
            prefilter=ctx.spec.prefilter,
        )
    return frame_search(
        target.table_for(binding.table),
        target,
        vec,
        column=binding.column,
        n=n,
        where=ctx.where,
        scope_where=ctx.spec.where,
    )


def _search_fts(ctx: SearchContext) -> list[dict[str, Any]]:
    """BM25 over the declared FTS column — phrase or fuzzy match per the spec."""
    from lancedb.query import MatchQuery, PhraseQuery

    spec = ctx.spec
    fts = _fts_binding(ctx.target)
    if spec.phrase:
        fts_query = PhraseQuery(spec.q, fts.column)
    else:
        fts_query = MatchQuery(spec.q, fts.column, fuzziness=spec.fuzziness)
    try:
        qb = ctx.target.row_tbl.search(fts_query).select(["_score", *ctx.target.payload_columns]).limit(spec.n)
        if ctx.where:
            qb = qb.where(ctx.where, prefilter=spec.prefilter)
        raw = qb.to_list()
    except Exception as e:
        logger.warning("fts search failed", exc_info=True)
        raise ValidationError("search failed") from e
    return _maybe_rerank(ctx, raw)


def _search_vector_mode(ctx: SearchContext) -> list[dict[str, Any]]:
    """Cosine over the DECLARED vector space the mode names — one uniform path for
    every embedding column (text / pixel / audio / any future modality).

    Behaviour is derived from the binding, never a role name: a space on the row
    table is searched directly; one on a frame table is ranked there and joined
    back by the identity keys (``_vector_leg``). Text-rerank applies only to
    text-queryable spaces — an image space keeps its own similarity order (the
    cross-encoder has no query text to score). The dispatcher only routes here
    when the mode IS a declared vector key, so the binding is present.
    """
    binding = ctx.target.binding(ctx.spec.mode)
    if binding is None:
        return []
    vec = _query_vec(ctx, binding)
    if vec is None:
        # The mode names a space the search box can't drive (foreign encoder, or
        # no query text/image supplied) — nothing to rank, so degrade to empty.
        return []
    hits = _vector_leg(ctx, binding, vec, n=ctx.spec.n)
    return _maybe_rerank(ctx, hits) if binding.query_encoder != "image" else hits


def _search_vector_fts_mode(ctx: SearchContext) -> list[dict[str, Any]]:
    """BM25 over a vector space's ``caption_source`` text (mode ``<key>_fts``),
    joined back to the row table — generalises the old ``scene_fts``."""
    key = ctx.spec.mode[: -len("_fts")]
    binding = ctx.target.binding(key)
    if binding is None or not binding.caption_source:
        return []
    hits = frame_fts_search(
        ctx.target.table_for(binding.table),
        ctx.target,
        ctx.spec.q,
        column=binding.caption_source,
        n=ctx.spec.n,
        where=ctx.where,
        scope_where=ctx.spec.where,
    )
    return _maybe_rerank(ctx, hits)


def _row_text_binding(target: SearchTarget) -> VectorBinding | None:
    """The text-encoder vector space that lives on the row table — the leg Lance
    fuses with FTS in a hybrid query (both must sit on one table).

    Prefers a space keyed ``semantic`` (the convention) but falls back to the
    first text space on the row table, so hybrid works for any text-embedding
    dataset, not only one that happens to name its space ``semantic``.
    """
    semantic = target.binding(SearchMode.SEMANTIC)
    if semantic is not None and semantic.table == target.row_table_name:
        return semantic
    for binding in target.vectors.values():
        if binding.query_encoder == "text" and binding.table == target.row_table_name:
            return binding
    return None


def _search_hybrid(ctx: SearchContext) -> list[dict[str, Any]]:
    """Lance-native FTS + text-vector fusion (RRF, or Linear when weighted)."""
    spec = ctx.spec
    target = ctx.target
    if ctx.text_vec is None:
        raise ValidationError("hybrid requires text query")
    fts = _fts_binding(target)
    binding = _row_text_binding(target)
    if binding is None:
        raise ValidationError("text embeddings are not built for this dataset")
    try:
        from lancedb.query import MatchQuery, PhraseQuery
        from lancedb.rerankers import LinearCombinationReranker, RRFReranker

        # Fusion: LinearCombination with the user's weight (the Balance slider:
        # 0 = pure FTS, 1 = pure vector, 0.5 = balanced) when supplied, else
        # parameter-free RRF. The cross-encoder rerank, if enabled, is applied to
        # the head afterwards so the rerank window stays independent of the
        # result count.
        fusion = LinearCombinationReranker(weight=spec.weight) if spec.weight is not None else RRFReranker()
        # FTS leg honours phrase/fuzziness like the dedicated 'fts' branch.
        if spec.phrase:
            fts_query = PhraseQuery(spec.q, fts.column)
        else:
            fts_query = MatchQuery(spec.q, fts.column, fuzziness=spec.fuzziness)
        # Name the vector column explicitly: the row table may carry several
        # vector columns, so Lance's hybrid query can't auto-pick which to search.
        qb = (
            target.row_tbl.search(query_type="hybrid", vector_column_name=binding.column)
            .vector(ctx.text_vec.tolist())
            .text(fts_query)
            .rerank(fusion)
            .minimum_nprobes(VECTOR_NPROBES)
            .maximum_nprobes(VECTOR_MAX_NPROBES)
            .refine_factor(VECTOR_REFINE_FACTOR)
            .select(target.payload_columns)
            .limit(spec.n)
        )
        if ctx.where:
            qb = qb.where(ctx.where, prefilter=spec.prefilter)
        raw = qb.to_list()
    except Exception as e:
        logger.warning("hybrid search failed", exc_info=True)
        raise ValidationError("hybrid search failed") from e
    return _maybe_rerank(ctx, raw)


def _search_all(ctx: SearchContext) -> list[dict[str, Any]]:
    """Fuse the FTS + semantic + visual + scene legs via RRF (whatever exists)."""
    spec = ctx.spec
    target = ctx.target
    rankings: list[list[dict[str, Any]]] = []
    from lancedb.query import MatchQuery

    # FTS leg (only if we have text and a row-table FTS binding).
    fts = target.fts
    if spec.q and fts is not None and fts.table == target.row_table_name:
        try:
            qb = target.row_tbl.search(MatchQuery(spec.q, fts.column, fuzziness=spec.fuzziness)).select(["_score", *target.payload_columns]).limit(spec.n * 3)
            if ctx.where:
                qb = qb.where(ctx.where, prefilter=spec.prefilter)
            rankings.append(qb.to_list())
        except Exception:
            pass

    # One vector leg per DECLARED embedding space the search box can drive —
    # uniform across modalities. `_query_vec` picks the right query vector (image
    # for an image encoder, text otherwise) or None for a space we can't drive
    # (foreign encoder), which is skipped. Each leg drops on failure — like the
    # FTS leg above — so one wrong-dimension / unbuilt space can't 400 the whole
    # fused search, it just contributes nothing.
    for binding in target.vectors.values():
        vec = _query_vec(ctx, binding)
        if vec is None:
            continue
        try:
            rankings.append(_vector_leg(ctx, binding, vec, n=spec.n * 3))
        except Exception:
            logger.warning("vector leg %r dropped from 'all' search", binding.column, exc_info=True)

    fused = rrf_fuse(rankings, key_fields=target.key_fields)
    # Optional cross-encoder rerank on the fused head (rerank_n), then trim to
    # spec.n. Without rerank we just take the fused top-n.
    return _maybe_rerank(ctx, fused)[: spec.n]


#: Composite / text-only modes with dedicated handlers. Every OTHER mode is a
#: declared vector-space key (→ _search_vector_mode) or '<key>_fts' (→
#: _search_vector_fts_mode), so adding an embedding column adds a mode with no
#: change here — the search is uniform across modalities.
_COMPOSITE_HANDLERS: dict[str, Callable[[SearchContext], list[dict[str, Any]]]] = {
    SearchMode.FTS.value: _search_fts,
    SearchMode.HYBRID.value: _search_hybrid,
    SearchMode.ALL.value: _search_all,
}


def _dispatch(ctx: SearchContext) -> list[dict[str, Any]]:
    """Route a mode: a composite (fts/hybrid/all), a ``<key>_fts`` BM25 leg, or a
    declared vector space. A mode the dataset does not declare yields NO results
    (graceful) — every embedding space is optional and uniform, so an absent one
    is simply empty, not an error. (The frontend only offers declared modes.)"""
    mode = ctx.spec.mode
    composite = _COMPOSITE_HANDLERS.get(mode)
    if composite is not None:
        return composite(ctx)
    if mode.endswith("_fts"):
        return _search_vector_fts_mode(ctx)
    return _search_vector_mode(ctx)


def _mode_needs_query_vector(target: SearchTarget, mode: str) -> bool:
    """Whether a mode ranks by a query embedding (so run_search must embed it).
    Any declared vector key does; fts and '<key>_fts' (BM25 on caption text)
    don't; hybrid/all always do."""
    if mode in (SearchMode.HYBRID.value, SearchMode.ALL.value):
        return True
    if mode.endswith("_fts"):
        return False
    return target.binding(mode) is not None


def run_search(
    handle: DatasetHandle,
    *,
    get_embedder: Callable[[], EmbeddingClient],
    get_reranker: Callable[[], VLLMReranker],
    spec: SearchSpec,
    filters: Mapping[str, str] | None = None,
    image_bytes: bytes | None,
) -> list[dict[str, Any]]:
    """Mode-aware search router over one dataset.

    ``filters`` maps descriptor-declared filterable field names to values (the
    router extracts them from the request). All paths return the same hit shape
    (``alignments`` parsed, ``caption`` attached when declared). The client
    getters raise :class:`ServiceUnavailableError` (HTTP 503) when their vLLM
    server is unreachable.
    """
    target = resolve_target(handle)
    where = build_where_clause(
        filters=filters or {},
        filterable=target.filterable,
        topic_columns=target.topic_columns,
        raw=spec.where,
    )

    # ── Filter-only browse (no query text or image) — list rows matching the
    # WHERE clause, e.g. a topic facet clicked on the Tree page. Nothing to
    # rank, so it's a plain Lance scan; no filter means nothing to list. ──
    if not spec.q and not spec.q_vec and image_bytes is None:
        if not where:
            return []
        try:
            raw = target.row_ds.to_table(columns=target.payload_columns, filter=where, limit=spec.n).to_pylist()
        except Exception as e:
            logger.warning("browse scan failed", exc_info=True)
            raise ValidationError("browse failed") from e
        return postprocess_hits(raw, target)

    # Build query vector(s) for the modes that rank by them. Connection /
    # network errors collapse into a structured 503 so the frontend shows a
    # meaningful message instead of "Internal Server Error". The vector leg may
    # use a distinct query string (spec.q_vec); the FTS leg always uses spec.q.
    text_vec = None
    image_vec = None
    if _mode_needs_query_vector(target, spec.mode):
        client = get_embedder()
        vec_text = spec.q_vec or spec.q  # empty q_vec falls back to q
        try:
            if vec_text:
                text_vec = client.embed_text([vec_text])[0]
            if image_bytes:
                image_vec = client.embed_image([image_bytes])[0]
        except Exception as e:
            logger.warning("embedding request failed", exc_info=True)
            raise ServiceUnavailableError("embedding service unavailable") from e

    ctx = SearchContext(
        target=target,
        get_reranker=get_reranker,
        spec=spec,
        where=where,
        rerank_query=" ".join(p for p in (spec.q, spec.q_vec) if p),
        text_vec=text_vec,
        image_vec=image_vec,
    )
    return postprocess_hits(_dispatch(ctx), target)
