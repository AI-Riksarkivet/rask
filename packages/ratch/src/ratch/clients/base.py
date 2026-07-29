"""Shared HTTP transport for the vLLM OpenAI-style model servers.

Every model client (embedding / reranker / caption / summarize) POSTs JSON to a
long-running vLLM server and fans concurrent calls out over a thread pool —
vLLM's continuous batching fuses them into one GPU pass, so each client maps its
items across a pool sized to saturate the GPU. This transport owns the httpx
connection pool and the fan-out so the clients only shape requests + parse replies.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, TypeVar

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
