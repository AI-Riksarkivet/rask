"""Ray composition root — bind registry stages to concrete compute factories.

The drivers in :mod:`ratch.core.driver` take a zero-arg ``factory`` that each
Ray actor calls once in its constructor; the factory builds the warm client and
returns the batch-compute callable. Everything here is a module-level function
so ``functools.partial(fn, url=...)`` pickles by reference — actors construct
their OWN clients, nothing client-side crosses the object store.

This module is the ONLY place stage names meet client constructors (the
old ``_run_*`` dispatchers' role, restated for Ray).
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import pyarrow as pa

from ratch.core.registry import Stage, StageShape
from ratch.features.embed_columns import VECTOR_TYPE, _vectors_to_arrow


if TYPE_CHECKING:
    from collections.abc import Callable


def text_embed_compute(url: str) -> Callable[[pa.Table], pa.Array]:
    from ratch.clients.embedding import VLLMEmbeddingClient

    client = VLLMEmbeddingClient(url)

    def compute(batch: pa.Table) -> pa.Array:
        return _vectors_to_arrow(client.embed_text([t or "" for t in batch.column("text").to_pylist()]))

    return compute


def caption_embed_compute(url: str) -> Callable[[pa.Table], pa.Array]:
    from ratch.clients.embedding import VLLMEmbeddingClient

    client = VLLMEmbeddingClient(url)

    def compute(batch: pa.Table) -> pa.Array:
        return _vectors_to_arrow(client.embed_text([c or "" for c in batch.column("caption").to_pylist()]))

    return compute


def summary_compute(url: str) -> Callable[[pa.Table], pa.Array]:
    from ratch.clients.summarize import VLLMSummarizeClient

    client = VLLMSummarizeClient(url)

    def compute(batch: pa.Table) -> pa.Array:
        return pa.array(client.summarize([t or "" for t in batch.column("text").to_pylist()]), pa.string())

    return compute


def image_embed_compute(url: str) -> Callable[[list[bytes]], pa.Array]:
    from ratch.clients.embedding import VLLMEmbeddingClient

    client = VLLMEmbeddingClient(url)

    def compute(payloads: list[bytes]) -> pa.Array:
        return _vectors_to_arrow(client.embed_image(payloads))

    return compute


def caption_compute(url: str, model: str, instruction: str | None) -> Callable[[list[bytes]], pa.Array]:
    from ratch.clients.caption import CAPTION_INSTRUCTION, VLLMCaptionClient

    client = VLLMCaptionClient(url, model=model, instruction=instruction or CAPTION_INSTRUCTION)

    def compute(payloads: list[bytes]) -> pa.Array:
        return pa.array(client.caption(payloads), pa.string())

    return compute


def bind_column_stage(
    stage: Stage,
    *,
    embed_url: str,
    summarize_url: str,
    caption_url: str,
    caption_model: str,
    caption_instruction: str | None = None,
) -> tuple[Callable[[], Callable], pa.DataType]:
    """The (factory, output type) pair for a SCAN/BLOB column stage."""
    match stage.name:
        case "text_embedding":
            return partial(text_embed_compute, embed_url), VECTOR_TYPE
        case "caption_embedding":
            return partial(caption_embed_compute, embed_url), VECTOR_TYPE
        case "summary":
            return partial(summary_compute, summarize_url), pa.string()
        case "frame_embedding":
            return partial(image_embed_compute, embed_url), VECTOR_TYPE
        case "caption":
            return partial(caption_compute, caption_url, caption_model, caption_instruction), pa.string()
    if stage.shape in (StageShape.SCAN_COLUMN, StageShape.BLOB_COLUMN):
        raise ValueError(f"no client binding for column stage {stage.name!r}")
    raise ValueError(f"stage {stage.name!r} is not a column stage")
