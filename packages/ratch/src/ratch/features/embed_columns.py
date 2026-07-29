"""Client-injectable column **builders** — the seam tests drive with a fake.

Each function here wraps the type-agnostic engine (:mod:`ratch.core.engine`)
with exactly one model client and writes one derived column (a vector or a
string) to a Lance table. They take an already-constructed client so an offline
fake can stand in; the production client is built from a server URL by the thin
``_run_*`` dispatchers in :mod:`ratch.features.columns`.

These are split out of ``columns.py`` so that module stays a registry + dispatch
layer and the heavy per-column compute lives here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pyarrow as pa

from ratch.core.engine import upsert_blob_column, upsert_scan_column

from ..model.schema import EMBED_DIM


if TYPE_CHECKING:
    from ratch.clients.caption import CaptionClient
    from ratch.clients.embedding import EmbeddingClient
    from ratch.clients.summarize import SummarizeClient

logger = logging.getLogger(__name__)

TEXT_EMBED_COLUMN = "text_embedding"
FRAME_EMBED_COLUMN = "frame_embedding"
SUMMARY_COLUMN = "summary"
CAPTION_COLUMN = "caption"
CAPTION_EMBED_COLUMN = "caption_embedding"

CHUNK_KEYS = ["doc_id", "speech_id", "chunk_id"]
# chunk_frames adds frame_idx to the key (a chunk may hold several frames).
FRAME_KEYS = ["doc_id", "speech_id", "chunk_id", "frame_idx"]
VECTOR_TYPE = pa.list_(pa.float32(), EMBED_DIM)


def _vectors_to_arrow(vectors: np.ndarray) -> pa.FixedSizeListArray:
    """``(N, EMBED_DIM)`` float32 array → Arrow ``FixedSizeList<float32, EMBED_DIM>``."""
    if vectors.ndim != 2 or vectors.shape[1] != EMBED_DIM:
        raise ValueError(f"expected (N, {EMBED_DIM}) vectors, got {vectors.shape}")
    flat = pa.array(np.ascontiguousarray(vectors, dtype=np.float32).reshape(-1), pa.float32())
    return pa.FixedSizeListArray.from_arrays(flat, EMBED_DIM)


def embed_text_column(
    chunks_path: str | Path,
    *,
    client: EmbeddingClient,
    batch_rows: int = 256,
    checkpoint_file: str | Path | None = None,
    overwrite: bool = False,
    progress: Callable[[int], None] | None = None,
) -> int:
    """Attach ``text_embedding`` (2048-d) to the chunks table from ``text``."""

    def compute(batch: pa.RecordBatch) -> pa.Array:
        return _vectors_to_arrow(client.embed_text([t or "" for t in batch.column("text").to_pylist()]))

    return upsert_scan_column(
        chunks_path,
        name=TEXT_EMBED_COLUMN,
        output_type=VECTOR_TYPE,
        key_columns=CHUNK_KEYS,
        read_columns=["text"],
        compute=compute,
        batch_rows=batch_rows,
        checkpoint_file=checkpoint_file,
        overwrite=overwrite,
        progress=progress,
    )


def embed_frame_column(
    frames_path: str | Path,
    *,
    client: EmbeddingClient,
    batch_rows: int = 256,
    checkpoint_file: str | Path | None = None,
    overwrite: bool = False,
    progress: Callable[[int], None] | None = None,
) -> int:
    """Attach ``frame_embedding`` (2048-d) to the chunk_frames table from ``frame_blob``."""

    def compute(jpegs: list[bytes]) -> pa.Array:
        return _vectors_to_arrow(client.embed_image(jpegs))

    return upsert_blob_column(
        frames_path,
        name=FRAME_EMBED_COLUMN,
        output_type=VECTOR_TYPE,
        blob_column="frame_blob",
        compute=compute,
        batch_rows=batch_rows,
        checkpoint_file=checkpoint_file,
        overwrite=overwrite,
        progress=progress,
    )


def summary_column(
    chunks_path: str | Path,
    *,
    client: SummarizeClient,
    batch_rows: int = 256,
    checkpoint_file: str | Path | None = None,
    overwrite: bool = False,
    progress: Callable[[int], None] | None = None,
) -> int:
    """Attach a one-line ``summary`` string to the chunks table from ``text``."""

    def compute(batch: pa.RecordBatch) -> pa.Array:
        return pa.array(client.summarize([t or "" for t in batch.column("text").to_pylist()]), pa.string())

    return upsert_scan_column(
        chunks_path,
        name=SUMMARY_COLUMN,
        output_type=pa.string(),
        key_columns=CHUNK_KEYS,
        read_columns=["text"],
        compute=compute,
        batch_rows=batch_rows,
        checkpoint_file=checkpoint_file,
        overwrite=overwrite,
        progress=progress,
    )


def caption_column(
    frames_path: str | Path,
    *,
    client: CaptionClient,
    batch_rows: int = 256,
    checkpoint_file: str | Path | None = None,
    overwrite: bool = False,
    progress: Callable[[int], None] | None = None,
) -> int:
    """Attach a ``caption`` string to the chunk_frames table from ``frame_blob``."""

    def compute(jpegs: list[bytes]) -> pa.Array:
        return pa.array(client.caption(jpegs), pa.string())

    return upsert_blob_column(
        frames_path,
        name=CAPTION_COLUMN,
        output_type=pa.string(),
        blob_column="frame_blob",
        compute=compute,
        batch_rows=batch_rows,
        checkpoint_file=checkpoint_file,
        overwrite=overwrite,
        progress=progress,
    )


def embed_caption_column(
    frames_path: str | Path,
    *,
    client: EmbeddingClient,
    batch_rows: int = 256,
    checkpoint_file: str | Path | None = None,
    overwrite: bool = False,
    progress: Callable[[int], None] | None = None,
) -> int:
    """Attach ``caption_embedding`` (2048-d) to chunk_frames from the ``caption`` text.

    The text counterpart to ``frame_embedding``: it embeds each frame's Swedish
    caption string (produced by :func:`caption_column`) into the same shared
    2048-d space, so a text query can retrieve frames by *what the scene depicts*
    (``mode=scene``), complementing the raw image-similarity ``frame_embedding``.
    Reads the existing ``caption`` column — it never re-reads or re-extracts the
    frame JPEGs. Run ``ratch feature caption`` first.
    """
    import lance

    ds = lance.dataset(str(frames_path))
    if CAPTION_COLUMN not in ds.schema.names:
        raise ValueError(f"'{CAPTION_COLUMN}' column missing on {frames_path} — run `ratch feature caption` before `caption_embedding`.")

    def compute(batch: pa.RecordBatch) -> pa.Array:
        captions = [c or "" for c in batch.column(CAPTION_COLUMN).to_pylist()]
        return _vectors_to_arrow(client.embed_text(captions))

    return upsert_scan_column(
        frames_path,
        name=CAPTION_EMBED_COLUMN,
        output_type=VECTOR_TYPE,
        key_columns=FRAME_KEYS,
        read_columns=[CAPTION_COLUMN],
        compute=compute,
        batch_rows=batch_rows,
        checkpoint_file=checkpoint_file,
        overwrite=overwrite,
        progress=progress,
    )


def chunk_frame_embedding_column(
    chunks_path: str | Path,
    frames_path: str | Path,
    *,
    column: str = FRAME_EMBED_COLUMN,
    frame_idx: int = 0,
    batch_rows: int = 4096,
    overwrite: bool = False,
    progress: Callable[[int], None] | None = None,
) -> int:
    """Attach a CHUNK-level copy of a per-frame vector ``column`` to ``chunks``.

    The visual/caption atlases project a chunk-level vector, but the source
    (``frame_embedding`` / ``caption_embedding``) lives PER-FRAME on
    ``chunk_frames`` (keyed by ``…/frame_idx``). This is a pure Lance scan+join —
    NO re-embedding: it reads the representative frame's (``frame_idx=0``, the
    same frame the UI/captions/``/chunk-frame`` already treat as canonical)
    ``column`` and attaches it to ``chunks`` keyed on
    ``(doc_id, speech_id, chunk_id)`` via ``add_columns``.

    Returns the number of chunk rows that received a vector (a chunk with no
    matching representative frame stays ``NULL``). Raises if ``chunk_frames``
    lacks ``column`` (run the matching ``ratch feature`` step first).
    """
    import lance

    frames_ds = lance.dataset(str(frames_path))
    if column not in frames_ds.schema.names:
        raise ValueError(f"'{column}' column missing on {frames_path} — run the matching `ratch feature {column}` step before the chunk-level join.")

    chunks_ds = lance.dataset(str(chunks_path))
    if column in chunks_ds.schema.names:
        if not overwrite:
            logger.info("%s already on chunks — nothing to do (pass overwrite=True)", column)
            return 0
        chunks_ds.drop_columns([column])
        chunks_ds = lance.dataset(str(chunks_path))

    # Build a {chunk key → representative-frame vector} map from one filtered scan.
    rep = frames_ds.to_table(columns=[*CHUNK_KEYS, column], filter=f"frame_idx = {int(frame_idx)}")
    vec_by_key: dict[tuple[str, int, int], list[float]] = {}
    docs = rep.column("doc_id").to_pylist()
    speeches = rep.column("speech_id").to_pylist()
    chunk_ids = rep.column("chunk_id").to_pylist()
    vectors = rep.column(column).to_pylist()
    for d, s, c, v in zip(docs, speeches, chunk_ids, vectors, strict=True):
        if v is not None:
            vec_by_key[(d, int(s), int(c))] = v
    logger.info(
        "loaded %d representative-frame %s vector(s) (frame_idx=%d) for the chunk-level join",
        len(vec_by_key),
        column,
        frame_idx,
    )

    schema = pa.schema([pa.field(column, VECTOR_TYPE, nullable=True)])
    matched = 0

    @lance.batch_udf(output_schema=schema)
    def attach(batch: pa.RecordBatch) -> pa.RecordBatch:
        nonlocal matched
        bdocs = batch.column("doc_id").to_pylist()
        bspeech = batch.column("speech_id").to_pylist()
        bchunk = batch.column("chunk_id").to_pylist()
        values: list[list[float] | None] = []
        for d, s, c in zip(bdocs, bspeech, bchunk, strict=True):
            v = vec_by_key.get((d, int(s), int(c)))
            if v is not None:
                matched += 1
            values.append(v)
        out = pa.array(values, type=VECTOR_TYPE)
        if progress is not None:
            progress(batch.num_rows)
        return pa.RecordBatch.from_arrays([out], names=[column])

    logger.info("attaching chunk-level %s via add_columns", column)
    chunks_ds.add_columns(attach, read_columns=CHUNK_KEYS, batch_size=batch_rows)
    return matched
