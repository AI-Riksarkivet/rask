"""Lazy vLLM query-encoder accessors bound to :class:`~service_kit.media.state.AppState`.

Ported from ``backend.clients`` onto the vendored encoders: each accessor
returns the cached client if present, else constructs it on first use and
writes it back onto the same ``AppState`` (so subsequent requests reuse it),
mapping any failure to a ``ServiceUnavailableError`` (HTTP 503). The URLs come
from ``state.settings``; the embedding client's ``expected_dim`` comes from the
default dataset's semantic vector binding — the descriptor, not a constant.
The encoder imports are deferred *inside* each function: it keeps FTS-only
startup free of the multimodal deps (numpy/Pillow), and it's the seam tests
monkeypatch (the symbol must resolve at call time, not import time).
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any

from search.services.spec import SearchMode
from service_kit.media.exceptions import ServiceUnavailableError
from service_kit.media.state import dataset_handle


if TYPE_CHECKING:
    from collections.abc import Callable

    from search.services.encoders.embedding import EmbeddingClient
    from search.services.encoders.reranker import VLLMReranker
    from service_kit.media.state import AppState

logger = logging.getLogger(__name__)


def _construct(factory: Callable[..., Any], available: dict[str, Callable[[], Any]]) -> Any:
    """Build a client, injecting each named kwarg only if the factory takes it.

    The real encoder clients accept ``embed_url=``/``rerank_url=`` (+
    ``expected_dim=`` for the embedder). Test fakes monkeypatched over the
    client class typically take no such parameters; we detect that via the
    signature and skip the injection so it never breaks them. The values are
    lazy thunks so a skipped kwarg costs nothing (the dim lookup opens the
    default dataset).
    """
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs = {name: make() for name, make in available.items() if name in params}
    return factory(**kwargs)


def _default_embed_dim(state: AppState) -> int:
    """The query-vector dimension of the default dataset's semantic binding.

    Falls back to the first declared vector binding when no ``semantic`` mode
    exists (any binding shares the encoder's output space); raises when the
    dataset declares no vectors at all — constructing an embedder then is a
    caller bug surfaced as the same 503 the accessor maps everything to.
    """
    handle = dataset_handle(state)
    search = handle.descriptor.declared.search
    if search is None or not search.vectors:
        raise RuntimeError("default dataset declares no vector bindings")
    binding = search.vectors.get(SearchMode.SEMANTIC.value) or next(iter(search.vectors.values()))
    return binding.dim


def ensure_embedder(state: AppState) -> EmbeddingClient:
    """Return the app's embedding client, connecting on first use (503 on failure)."""
    if state.embedder is not None:
        return state.embedder
    try:
        from search.services.encoders.embedding import VLLMEmbeddingClient

        state.embedder = _construct(
            VLLMEmbeddingClient,
            {
                "embed_url": lambda: state.settings.embed_url,
                "expected_dim": lambda: _default_embed_dim(state),
            },
        )
        return state.embedder
    except Exception as e:
        logger.exception("failed to initialize embedding client")
        raise ServiceUnavailableError("embedding service unavailable") from e


def ensure_reranker(state: AppState) -> VLLMReranker:
    """Return the app's reranker client, connecting on first use (503 on failure)."""
    if state.reranker is not None:
        return state.reranker
    try:
        from search.services.encoders.reranker import VLLMReranker

        state.reranker = _construct(VLLMReranker, {"rerank_url": lambda: state.settings.rerank_url})
        return state.reranker
    except Exception as e:
        logger.exception("failed to initialize reranker client")
        raise ServiceUnavailableError("rerank service unavailable") from e
