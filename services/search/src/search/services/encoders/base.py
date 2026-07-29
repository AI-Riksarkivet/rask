"""Shared HTTP transport + wire shapes for the vLLM OpenAI-style model servers.

Vendored from ``packages/ratch/clients/base.py`` and the request/response models of
``packages/ratch/clients/schemas.py`` (backend split, §4.4: no ``ratch`` imports in
``backend/``). Every model client POSTs JSON to a long-running vLLM server and
fans concurrent calls out over a thread pool — vLLM's continuous batching fuses
them into one GPU pass. This transport owns the httpx connection pool and the
fan-out so the clients only shape requests + parse replies.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import httpx
from pydantic import BaseModel


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

DEFAULT_TIMEOUT_S = 120.0

_Resp = TypeVar("_Resp", bound=BaseModel)
_In = TypeVar("_In")
_Out = TypeVar("_Out")


class VLLMTransport:
    """POST JSON to a vLLM server, with a pooled client and concurrent fan-out."""

    def __init__(self, base_url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S, pool_size: int = 32) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            timeout=timeout_s,
            limits=httpx.Limits(max_connections=pool_size * 2, max_keepalive_connections=pool_size),
        )

    def close(self) -> None:
        """Release the pooled HTTP connections."""
        self._http.close()

    def post(self, path: str, body: dict[str, Any], *, into: type[_Resp]) -> _Resp:
        """POST ``body`` to ``{base_url}{path}`` and validate the JSON reply into ``into``.

        Parsing the reply at the transport boundary into a Pydantic model means
        every client gets a typed response instead of indexing ``dict[str, Any]``.
        """
        r = self._http.post(f"{self.base_url}{path}", json=body)
        r.raise_for_status()
        return into.model_validate(r.json())

    def map(self, fn: Callable[[_In], _Out], items: Iterable[_In], *, concurrency: int) -> list[_Out]:
        """Run ``fn`` over ``items`` across a thread pool, preserving input order."""
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            return list(pool.map(fn, items))


# ── Request: OpenAI-style chat messages ──────────────────────────────────────


class ImageUrl(BaseModel):
    """The ``image_url`` payload of an image content part (a ``data:`` / http URL)."""

    url: str


class TextPart(BaseModel):
    """A text block in a multimodal message's ``content`` list."""

    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    """An image block in a multimodal message's ``content`` list."""

    type: Literal["image_url"] = "image_url"
    image_url: ImageUrl


#: One content block — text or image — of a multimodal chat message.
ContentPart = TextPart | ImagePart


class ChatMessage(BaseModel):
    """One OpenAI-style chat turn.

    ``content`` is either a plain string (text-only LLMs) or a list of typed parts
    (vision models); ``model_dump()`` reproduces the exact wire shape the servers
    expect, so callers build ``ChatMessage`` objects and dump them into the body.
    """

    role: Literal["system", "user", "assistant"]
    content: str | list[ContentPart]


# ── Response: only the fields the clients read (extras ignored) ───────────────


class _EmbeddingItem(BaseModel):
    embedding: list[float]


class EmbeddingResponse(BaseModel):
    """``POST /v1/embeddings`` reply — the embedding client reads ``data``."""

    data: list[_EmbeddingItem]


class RerankResult(BaseModel):
    """One scored candidate from ``POST /v1/rerank`` (may arrive unordered)."""

    index: int
    relevance_score: float


class RerankResponse(BaseModel):
    """``POST /v1/rerank`` reply — the reranker reads ``results``."""

    results: list[RerankResult]
