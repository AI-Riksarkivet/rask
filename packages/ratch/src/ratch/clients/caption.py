"""Frame captioning client: image → short text caption via a vLLM VLM server.

A generative counterpart to the embedding/reranker clients — it asks a
vision-language model (chat-completions) to describe each frame in one line. The
caption is stored as a plain ``string`` feature column on ``chunk_frames``; a
second feature (``caption_embedding``) then embeds those strings so the scene
description becomes vector-searchable (mode ``scene``).

Defaults target **Gemma 4** served by vLLM on port 8003 with an OpenAI-style
chat-completions API, producing **Swedish** captions for the Riksarkivet corpus.
Model / URL / instruction are overridable via the ``ratch feature caption``
CLI flags or the ``MEDIA_CAPTION_*`` env vars, so the same client can drive a
different VLM (e.g. ``Qwen/Qwen3-VL-Instruct-2B``) or language without code edits.
"""

from __future__ import annotations

import os
from typing import Protocol

from .base import DEFAULT_TIMEOUT_S, VLLMTransport
from .image import frame_to_data_url
from .schemas import ChatCompletionResponse, ChatMessage, ImagePart, ImageUrl, TextPart


# Gemma 4 instruction-tuned VLM that you already run locally at the OpenAI chat
# endpoint on :8003 (the `rask-gemma` server). This client only POSTs to it — it
# does not start a server. Override with MEDIA_CAPTION_* or the CLI flags.
CAPTION_MODEL = os.getenv("MEDIA_CAPTION_MODEL", "google/gemma-4-31B-it")
DEFAULT_CAPTION_URL = os.getenv("MEDIA_CAPTION_URL", "http://127.0.0.1:8003")
# Swedish, single factual sentence — the corpus is Swedish press/archival footage.
# Explicitly forbid the "En bild av… / Bilden visar…" preamble: it repeats across
# every caption, which both reads worse and dilutes the caption_embedding vectors
# (every vector shares that lead-in). Ask for the scene content directly.
CAPTION_INSTRUCTION = os.getenv(
    "MEDIA_CAPTION_INSTRUCTION",
    "Beskriv sakligt vad som syns i videobilden, i en kort mening på svenska. "
    'Börja direkt med motivet — inte med "En bild", "Bilden visar" eller liknande. '
    "Svara endast med den beskrivande meningen.",
)
CAPTION_CONCURRENCY = 8
# Headroom for a full Swedish sentence (Swedish tokenizes longer than English).
CAPTION_MAX_TOKENS = 96


class CaptionClient(Protocol):
    """Images → one caption string each. The contract the caption feature needs."""

    def caption(self, images: list[bytes]) -> list[str]: ...


class VLLMCaptionClient:
    """HTTP client for a vLLM chat-completions VLM server.

    ``POST {caption_url}/v1/chat/completions`` with one image per request; returns
    the assistant's text. Errors surface as :class:`httpx.HTTPError`.
    """

    def __init__(
        self,
        caption_url: str = DEFAULT_CAPTION_URL,
        *,
        model: str = CAPTION_MODEL,
        instruction: str = CAPTION_INSTRUCTION,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        concurrency: int = CAPTION_CONCURRENCY,
        max_tokens: int = CAPTION_MAX_TOKENS,
    ) -> None:
        self.model = model
        self.instruction = instruction
        self.max_tokens = max_tokens
        self.concurrency = concurrency
        self._t = VLLMTransport(caption_url, timeout_s=timeout_s, pool_size=concurrency)

    def close(self) -> None:
        """Release the transport's HTTP connection pool."""
        self._t.close()

    def caption(self, images: list[bytes]) -> list[str]:
        """Return one caption per image, in input order."""
        if not images:
            return []
        return self._t.map(self._caption_one, images, concurrency=self.concurrency)

    def _caption_one(self, image: bytes) -> str:
        messages = [
            ChatMessage(
                role="user",
                content=[
                    ImagePart(image_url=ImageUrl(url=frame_to_data_url(image))),
                    TextPart(text=self.instruction),
                ],
            )
        ]
        body = {
            "model": self.model,
            "messages": [m.model_dump() for m in messages],
            "max_tokens": self.max_tokens,
            "temperature": 0.0,
        }
        resp = self._t.post("/v1/chat/completions", body, into=ChatCompletionResponse)
        return resp.choices[0].message.content.strip()
