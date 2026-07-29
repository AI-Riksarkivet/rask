"""Pydantic value objects for the vLLM OpenAI-style request/response shapes.

The model clients (embedding / caption / summarize / reranker) build these and
parse server replies into them, so request construction and response access are
type-checked instead of indexing into ``dict[str, object]`` / ``dict[str, Any]``.
Only the fields the clients actually read are declared — Pydantic ignores the
rest of each reply (``id``, ``usage``, …) by default.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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


class _ChatChoiceMessage(BaseModel):
    content: str


class _ChatChoice(BaseModel):
    message: _ChatChoiceMessage


class ChatCompletionResponse(BaseModel):
    """``POST /v1/chat/completions`` reply — caption + summarize read ``choices``.

    ``min_length=1`` makes a malformed empty-``choices`` reply fail at the
    transport parse boundary (a clear ``ValidationError`` from ``post``) instead
    of an ``IndexError`` at the ``choices[0]`` read in each client.
    """

    choices: list[_ChatChoice] = Field(min_length=1)


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
