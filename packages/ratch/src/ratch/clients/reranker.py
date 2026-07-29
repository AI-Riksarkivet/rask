"""Cross-encoder reranker: re-orders the top-K hits via the vLLM
Qwen3-VL-Reranker-2B server. Counterpart to the bi-encoder in
:mod:`ratch.clients.embedding` — a bi-encoder embeds query and document
independently and compares vectors; a cross-encoder reads the (query, document)
pair together and scores relevance directly (slower, more accurate), so it only
re-orders what cheaper retrieval already found.

Why a custom reranker: LanceDB's ``.rerank()`` takes a
:class:`lancedb.rerankers.Reranker` subclass, and none of the built-ins call our
Qwen reranker server. :class:`QwenVLReranker` is the adapter that lets Lance's
hybrid pipeline delegate scoring to it over HTTP. Non-Lance paths (the API's
``mode=all``) call :meth:`VLLMReranker.rerank` directly.

The prompt scaffolding below is verbatim from the model card and mirrors
``retrieval/qwen3_vl_reranker.jinja`` (the vLLM chat template) — keep the two
byte-compatible; drift silently degrades rerank quality.
"""

from __future__ import annotations

import os

import pyarrow as pa
from lancedb.rerankers import Reranker

from .base import DEFAULT_TIMEOUT_S, VLLMTransport
from .schemas import RerankResponse


RERANK_MODEL = "Qwen/Qwen3-VL-Reranker-2B"
DEFAULT_RERANK_URL = os.getenv("MEDIA_RERANK_URL", "http://127.0.0.1:8002")
DEFAULT_TOP_K = 100  # rows sent to the cross-encoder; the rest keep fused order

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
        rerank_url: str = DEFAULT_RERANK_URL,
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


class QwenVLReranker(Reranker):
    """LanceDB reranker adapter that delegates scoring to a :class:`VLLMReranker`.

    Plugs into Lance's hybrid query::

        chunks.query().full_text_search(q).nearest_to(vec).rerank(
            QwenVLReranker(VLLMReranker())
        )

    Only the top ``top_k`` rows are sent to the cross-encoder; the tail keeps its
    fused order.
    """

    def __init__(self, client: VLLMReranker, top_k: int = DEFAULT_TOP_K) -> None:
        super().__init__()
        self.client = client
        self.top_k = top_k

    def _score(self, query: str, table: pa.Table) -> pa.Table:
        if table.num_rows == 0:
            return table.append_column("_relevance_score", pa.array([], type=pa.float32()))
        head = table.slice(0, min(self.top_k, table.num_rows))
        scores = self.client.rerank(query, head.column("text").to_pylist())
        scored = head.append_column("_relevance_score", pa.array(scores, type=pa.float32()))
        return scored.sort_by([("_relevance_score", "descending")])

    def rerank_hybrid(self, query: str, vector_results: pa.Table, fts_results: pa.Table) -> pa.Table:
        return self._score(query, self.merge_results(vector_results, fts_results))

    def rerank_vector(self, query: str, vector_results: pa.Table) -> pa.Table:
        return self._score(query, vector_results)

    def rerank_fts(self, query: str, fts_results: pa.Table) -> pa.Table:
        return self._score(query, fts_results)
