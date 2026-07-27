"""Cross-encoder head rerank: re-score the top ``rerank_n`` hits, keep the tail.

Ported from ``backend.search.rerank``; the scored text column now comes from the
descriptor's ``display.body`` instead of a hardcoded name. Pure: no Lance, no
exceptions. The reranker getter (passed in) is the only dependency, and that
getter owns its own 503 mapping (see :mod:`search.services.clients`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable

    from search.services.encoders.reranker import VLLMReranker


def rerank_by_text(
    get_reranker: Callable[[], VLLMReranker],
    query: str,
    hits: list[dict[str, Any]],
    *,
    body_column: str | None,
    rerank_n: int,
    n: int,
) -> list[dict[str, Any]]:
    """Cross-encoder rerank the top ``rerank_n`` of ``hits``, then return ``n``
    results: the reranked head followed by the remaining first-stage hits.

    Reranking only the head bounds the (slow) cross-encoder cost while letting
    the result list be longer than the reranked window. The reranker is
    text-only — it scores ``query`` jointly with each candidate's body text and
    ignores vectors/images, so with no query text (image-only search), no hits,
    or no declared body column it's a plain top-``n`` and the first-stage order
    is preserved.
    """
    if not query or not hits or not body_column:
        return hits[:n]
    head = hits[:rerank_n]
    tail = hits[rerank_n:]
    # `or ""`: frame-joined hits can carry a null body — the reranker rejects
    # null documents, and an empty string scores neutrally instead.
    scores = get_reranker().rerank(query, [h.get(body_column) or "" for h in head])
    head = [h for _, h in sorted(zip(scores, head, strict=False), key=lambda p: -p[0])]
    return (head + tail)[:n]
