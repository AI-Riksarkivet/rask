"""Bi-encoder embedding client: text + image → full 2048-d vectors via vLLM.

The *bi-encoder* half of retrieval — it encodes text and images independently
into vectors for Lance storage and cosine search. Its counterpart, the
cross-encoder reranker, lives in :mod:`ratch.clients.reranker`.

The model is Qwen3-VL-Embedding-2B, served by a long-running vLLM HTTP server
(`make embed-server`) so it loads once and stays warm across the feature CLI
(`ratch feature text_embedding` / `frame_embedding`) and the backend.
"""

from __future__ import annotations

import os
from typing import Protocol

import numpy as np

from ..model.schema import EMBED_DIM
from .base import DEFAULT_TIMEOUT_S, VLLMTransport
from .image import image_to_data_url, l2_normalize
from .schemas import ChatMessage, EmbeddingResponse, ImagePart, ImageUrl, TextPart


EMBED_MODEL = "Qwen/Qwen3-VL-Embedding-2B"
DEFAULT_EMBED_URL = os.getenv("MEDIA_EMBED_URL", "http://127.0.0.1:8001")

# Per the model card, an English instruction works best even for Swedish input.
EMBED_INSTRUCTION = "Represent the user's input."

# In-flight HTTP requests per call: vLLM's continuous batching fuses them into
# one GPU pass, so push as high as the GPU saturates (--max-num-seqs defaults to
# 256). Tune against nvidia-smi. Images cost more vision tokens, hence lower.
TEXT_CONCURRENCY = 32
IMAGE_CONCURRENCY = 8


class EmbeddingClient(Protocol):
    """Text/image → vectors. The contract the feature engine + backend depend on.

    :class:`VLLMEmbeddingClient` satisfies it in production; tests inject a
    deterministic offline fake. Declaring it structurally (not as the concrete
    class) keeps the embed pipeline unit-testable with no GPU or HTTP server.
    """

    def embed_text(self, texts: list[str]) -> np.ndarray: ...
    def embed_image(self, images: list[bytes]) -> np.ndarray: ...


class VLLMEmbeddingClient:
    """HTTP client for the vLLM Qwen3-VL-Embedding-2B server.

    ``POST {embed_url}/v1/embeddings`` with the Qwen-VL chat shape (system
    instruction + user text/image + empty assistant turn). The vLLM pooler
    returns raw 2048-d vectors, which we L2-normalize for cosine search. Errors
    surface as :class:`httpx.HTTPError`; callers wrap them into 503s.
    """

    def __init__(
        self,
        embed_url: str = DEFAULT_EMBED_URL,
        *,
        model: str = EMBED_MODEL,
        instruction: str = EMBED_INSTRUCTION,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        text_concurrency: int = TEXT_CONCURRENCY,
        image_concurrency: int = IMAGE_CONCURRENCY,
    ) -> None:
        self.model = model
        self.instruction = instruction
        self.text_concurrency = text_concurrency
        self.image_concurrency = image_concurrency
        self._t = VLLMTransport(embed_url, timeout_s=timeout_s, pool_size=max(text_concurrency, image_concurrency))

    def close(self) -> None:
        """Release the transport's HTTP connection pool."""
        self._t.close()

    def embed_text(self, texts: list[str]) -> np.ndarray:
        """Return an ``(N, EMBED_DIM) float32`` L2-normalized array."""
        if not texts:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        messages = [self._text_message(t) for t in texts]
        return l2_normalize(self._t.map(self._embed_one, messages, concurrency=self.text_concurrency))

    def embed_image(self, images: list[bytes]) -> np.ndarray:
        """Return an ``(N, EMBED_DIM) float32`` L2-normalized array (JPEG bytes in)."""
        if not images:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        messages = [self._image_message(img) for img in images]
        return l2_normalize(self._t.map(self._embed_one, messages, concurrency=self.image_concurrency))

    def _embed_one(self, messages: list[ChatMessage]) -> list[float]:
        body = {
            "model": self.model,
            "messages": [m.model_dump() for m in messages],
            "encoding_format": "float",
            "continue_final_message": True,
            "add_special_tokens": True,
        }
        return self._t.post("/v1/embeddings", body, into=EmbeddingResponse).data[0].embedding

    def _text_message(self, text: str) -> list[ChatMessage]:
        return [
            ChatMessage(role="system", content=[TextPart(text=self.instruction)]),
            ChatMessage(role="user", content=[TextPart(text=text)]),
            ChatMessage(role="assistant", content=[TextPart(text="")]),
        ]

    def _image_message(self, image: bytes) -> list[ChatMessage]:
        return [
            ChatMessage(role="system", content=[TextPart(text=self.instruction)]),
            ChatMessage(
                role="user",
                content=[
                    ImagePart(image_url=ImageUrl(url=image_to_data_url(image))),
                    # Qwen3-VL's pure-image examples include an empty trailing text block.
                    TextPart(text=""),
                ],
            ),
            ChatMessage(role="assistant", content=[TextPart(text="")]),
        ]
