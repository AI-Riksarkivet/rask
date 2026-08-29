"""Shared, app-wide resources built in lifespan and read via DI.

``AppState`` lives on ``app.state.resources`` and is reached through the
``StateDep`` dependency — routers never touch ``app.state`` directly. Data
resolution goes through :func:`dataset_handle` (the multi-dataset registry);
the encoder/voice slots stay ``None`` until first use so an FTS-only
deployment never connects to a GPU server.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from service_kit.media.config import Settings, get_settings


if TYPE_CHECKING:
    from service_kit.lancekit.registry import DatasetRegistry

logger = logging.getLogger(__name__)


class AppState(BaseModel):
    """Per-app resource handles + lazy vLLM client cache.

    The Lance/lancedb handles aren't Pydantic-validatable, hence
    ``arbitrary_types_allowed``. ``embedder``/``reranker`` are mutable slots that
    :mod:`search.services.clients` fills on first use and reuses thereafter. ``settings``
    carries the typed app config (vLLM URLs, host/port, CORS); it defaults via
    ``get_settings()`` so bare constructions in tests still work.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Kept as tolerated no-op ctor args during the split-transition so existing
    # constructions keep validating; the registry is the only data path.
    db_path: Path | None = None
    names: list[str] = Field(default_factory=list)
    chunks: Any = None
    settings: Settings = Field(default_factory=get_settings)
    embedder: Any | None = None  # search.services.encoders.embedding.VLLMEmbeddingClient
    reranker: Any | None = None  # search.services.encoders.reranker.VLLMReranker
    # Lazy in-process voice encoder for the upload voice search (CPU by design;
    # the GPUs belong to the vLLM servers). None until the first upload; the
    # lock guards first-load — sync handlers run in a threadpool, so two
    # concurrent first uploads would otherwise both construct the model.
    voice_encoder: Any | None = None  # viewer.services.wespeaker.VoiceEncoder
    voice_encoder_lock: Any = Field(default_factory=threading.Lock)
    # One HTTP connection pool per process (health pings) — never per-request.
    http: Any | None = None  # httpx.Client
    # Memoized /points payloads keyed on (space, dataset version) — per-app, not
    # module-global, so two app instances (e.g. tests) can't cross-contaminate.
    # Writes are idempotent (same input → same bytes), so a plain dict suffices.
    # Carries the payload's SIZE alongside it, like `search_cache` below, so eviction can honour a
    # byte ceiling without re-measuring every entry. The atlas payload is `bytes`, so the size is
    # EXACT rather than the estimate the search cache has to make.
    points_cache: dict[tuple[str, str, int], tuple[bytes, int]] = Field(default_factory=dict)
    # Version-keyed search result cache (search.services.result_cache): keyed
    # (dataset, table-version signature, query hash) → (the query's hits, their
    # approximate byte size — carried so eviction can honour the byte ceiling
    # without re-serializing every entry). Same per-app, version-invalidated
    # discipline as points_cache; bounded by settings.search_cache_size AND
    # settings.search_cache_bytes (0 = off / no byte bound respectively).
    search_cache: dict[tuple[str, str, str], tuple[list[Any], int]] = Field(default_factory=dict)
    # Multi-dataset registry (LANCE_MEDIA_MERGE §4.4) — the schema-agnostic
    # resolution path. The legacy per-table fields above stay during the
    # media/search service port and are stripped at integration. Same first-build
    # hazard as voice_encoder: sync handlers run in a threadpool, so two
    # concurrent first requests would otherwise both construct the registry.
    registry: Any | None = None  # service_kit.lancekit.registry.DatasetRegistry
    registry_lock: Any = Field(default_factory=threading.Lock)


def ensure_registry(state: AppState) -> DatasetRegistry:
    """The ONE lazy first-build of ``state.registry`` (SK-06).

    Every path that needs the registry — id resolution via :func:`dataset_handle`,
    the viewer's enumeration (which has no dataset id to resolve through) — goes
    through here, so the guarded construction exists exactly once. An unlocked
    copy of this block lived in the viewer and raced it; de-duplication, not a
    second lock, is what keeps the two from drifting again.
    """
    # Imported per call, not at module top: service-kit's media plane stays
    # importable without the Lance stack until a registry is actually needed.
    from service_kit.lancekit.registry import DatasetRegistry

    if state.registry is None:
        # Double-checked so the steady state never takes the lock; without it two
        # concurrent first requests both construct, and the loser's registry (with
        # whatever it already opened) is silently discarded.
        with state.registry_lock:
            if state.registry is None:
                state.registry = DatasetRegistry(
                    state.settings.registry_root,
                    state.settings.descriptor_dir,
                    state.settings.default_dataset_id,
                    storage_options=state.settings.storage_options(),
                )
    registry: DatasetRegistry = state.registry
    return registry


def dataset_handle(state: AppState, dataset_id: str | None = None):
    """Resolve a dataset by id through the registry (``None`` → the default).

    The one resolution path both router groups share; raises
    ``service_kit.exceptions.NotFoundError`` for unknown ids so the RFC 9457
    handler renders a clean 404.
    """
    from service_kit.exceptions import NotFoundError
    from service_kit.lancekit.registry import UnknownDatasetError

    registry = ensure_registry(state)
    try:
        return registry.get(dataset_id) if dataset_id else registry.default()
    except UnknownDatasetError as exc:
        raise NotFoundError(str(exc)) from exc
    except ValueError as exc:  # invalid/missing descriptor — a config problem, not a 500
        raise NotFoundError(str(exc)) from exc
