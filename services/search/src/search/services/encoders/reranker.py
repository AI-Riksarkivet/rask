"""Cross-encoder reranker client for the vLLM Qwen3-VL-Reranker-2B server.

Vendored from ``packages/ratch/clients/reranker.py`` (backend split, §4.4: no
``ratch`` imports in ``backend/``), trimmed to the plain HTTP client — the
LanceDB ``Reranker`` adapter stays in the pipeline package (the search service
calls :meth:`VLLMReranker.rerank` directly; hybrid fusion uses Lance's
built-ins). Counterpart to the bi-encoder in
:mod:`search.services.encoders.embedding`: a bi-encoder embeds query and
document independently and compares vectors; a cross-encoder reads the
(query, document) pair together and scores relevance directly (slower, more
accurate), so it only re-orders what cheaper retrieval already found.

The prompt scaffolding below is verbatim from the model card and mirrors the
vLLM chat template the rerank server is launched with — keep the two
byte-compatible; drift silently degrades rerank quality.
"""

from __future__ import annotations

from search.services.encoders.base import DEFAULT_TIMEOUT_S, RerankResponse, VLLMTransport


RERANK_MODEL = "Qwen/Qwen3-VL-Reranker-2B"

RERANK_INSTRUCTION = "Given a search query, retrieve relevant candidates that answer the query."
#: The score a candidate the server declined to rank receives — below any real [0, 1] relevance,
#: so an unscored document sinks to the bottom of the rerank order instead of stealing a rank.
_UNSCORED = -1.0

_PREFIX = (
    "<|im_start|>system\n"
    "Judge whether the Document meets the requirements based on the Query "
    'and the Instruct provided. Note that the answer can only be "yes" or "no".'
    "<|im_end|>\n<|im_start|>user\n"
)
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n"


class VLLMReranker:
    """HTTP client for the vLLM Qwen3-VL-Reranker-2B server.

    ``POST {rerank_url}/v1/rerank``. We wrap query + document in the model card's
    prefix/suffix scaffolding; the server (launched with
    ``classifier_from_token=["no", "yes"]``) returns relevance scores in [0, 1]
    (the "yes" probability).
    """

    def __init__(
        self,
        rerank_url: str,
        *,
        model: str = RERANK_MODEL,
        instruction: str = RERANK_INSTRUCTION,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.model = model
        self.instruction = instruction
        self._t = VLLMTransport(rerank_url, timeout_s=timeout_s, pool_size=1)

    def close(self) -> None:
        """Release the transport's HTTP connection pool."""
        self._t.close()

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        """Return one relevance score per candidate, in input order — always ``len(candidates)`` of them.

        The reply MAY be unordered, short (fewer scores than documents) or sparse (an index skipped),
        so the answer is rebuilt BY INDEX rather than by the reply's own order: `sorted()`-then-strip
        aligned scores to candidates only when the reply was dense and full-length, and otherwise
        handed candidate *i* candidate *j*'s score (VS-14). A candidate the server returned no score
        for gets `_UNSCORED` — a floor, so it ranks last rather than inheriting a neighbour's score;
        the reranker declining to score a document is not evidence it is relevant.
        """
        if not candidates:
            return []
        body = {
            "model": self.model,
            "query": f"{_PREFIX}<Instruct>: {self.instruction}\n<Query>: {query}\n",
            "documents": [f"<Document>: {c}{_SUFFIX}" for c in candidates],
        }
        results = self._t.post("/v1/rerank", body, into=RerankResponse).results
        by_index: dict[int, float] = {}
        for item in results:
            if not 0 <= item.index < len(candidates):
                raise ValueError(f"rerank server returned index {item.index} out of range for {len(candidates)} candidates")
            by_index[item.index] = item.relevance_score
        return [by_index.get(i, _UNSCORED) for i in range(len(candidates))]
