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
        """Return one relevance score per candidate, in input order."""
        if not candidates:
            return []
        body = {
            "model": self.model,
            "query": f"{_PREFIX}<Instruct>: {self.instruction}\n<Query>: {query}\n",
            "documents": [f"<Document>: {c}{_SUFFIX}" for c in candidates],
        }
        results = self._t.post("/v1/rerank", body, into=RerankResponse).results  # may be unordered
        scored = sorted((item.index, item.relevance_score) for item in results)
        return [score for _, score in scored]
