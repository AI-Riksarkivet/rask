"""Chunk summarization client: text → short summary via a vLLM LLM server.

A generative counterpart to the embedding client — it asks an instruct LLM
(chat-completions) to compress each chunk's transcript into one line. The summary
is stored as a plain ``string`` feature column on ``chunks`` and indexed for FTS.
"""

from __future__ import annotations

import os
from typing import Protocol

from .base import DEFAULT_TIMEOUT_S, VLLMTransport
from .schemas import ChatCompletionResponse, ChatMessage


SUMMARIZE_MODEL = "Qwen/Qwen3-Instruct-4B"
DEFAULT_SUMMARIZE_URL = os.getenv("MEDIA_SUMMARIZE_URL", "http://127.0.0.1:8004")
SUMMARIZE_INSTRUCTION = "Summarize the following transcript passage in one concise sentence."
SUMMARIZE_CONCURRENCY = 32
SUMMARIZE_MAX_TOKENS = 96


class SummarizeClient(Protocol):
    """Texts → one summary string each. The contract the summary feature needs."""

    def summarize(self, texts: list[str]) -> list[str]: ...


class VLLMSummarizeClient:
    """HTTP client for a vLLM chat-completions instruct-LLM server.

    ``POST {summarize_url}/v1/chat/completions`` with one passage per request;
    returns the assistant's text. Errors surface as :class:`httpx.HTTPError`.
    """

    def __init__(
        self,
        summarize_url: str = DEFAULT_SUMMARIZE_URL,
        *,
        model: str = SUMMARIZE_MODEL,
        instruction: str = SUMMARIZE_INSTRUCTION,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        concurrency: int = SUMMARIZE_CONCURRENCY,
        max_tokens: int = SUMMARIZE_MAX_TOKENS,
    ) -> None:
        self.model = model
        self.instruction = instruction
        self.max_tokens = max_tokens
        self.concurrency = concurrency
        self._t = VLLMTransport(summarize_url, timeout_s=timeout_s, pool_size=concurrency)

    def close(self) -> None:
        """Release the transport's HTTP connection pool."""
        self._t.close()

    def summarize(self, texts: list[str]) -> list[str]:
        """Return one summary per text, in input order."""
        if not texts:
            return []
        return self._t.map(self._summarize_one, texts, concurrency=self.concurrency)

    def _summarize_one(self, text: str) -> str:
        messages = [
            ChatMessage(role="system", content=self.instruction),
            ChatMessage(role="user", content=text),
        ]
        body = {
            "model": self.model,
            "messages": [m.model_dump() for m in messages],
            "max_tokens": self.max_tokens,
            "temperature": 0.0,
        }
        resp = self._t.post("/v1/chat/completions", body, into=ChatCompletionResponse)
        return resp.choices[0].message.content.strip()
