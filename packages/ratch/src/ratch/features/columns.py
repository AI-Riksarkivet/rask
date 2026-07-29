"""The ``FEATURES`` registry + the thin per-feature ``_run_*`` dispatchers.

Each :class:`Feature` maps a name to a ``run`` that builds the production model
client from a server URL and calls the matching client-injectable column builder
in :mod:`ratch.features.embed_columns` (the seam tests drive with an offline
fake). The ``ratch feature <name>`` CLI is a thin loop over this dict, so adding
a column is one entry here. The column constants and builder functions are
re-exported below so existing ``ratch.features.columns`` imports keep working.
"""

from __future__ import annotations

import logging

# Path + Callable are imported at runtime (not under TYPE_CHECKING) because the
# Pydantic ``Feature`` model resolves its ``run`` field annotation at class-build time.
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from ratch.core.engine import ensure_fts_index, ensure_vector_index

from .embed_columns import (
    CAPTION_COLUMN,
    CAPTION_EMBED_COLUMN,
    CHUNK_KEYS,
    FRAME_EMBED_COLUMN,
    FRAME_KEYS,
    SUMMARY_COLUMN,
    TEXT_EMBED_COLUMN,
    VECTOR_TYPE,
    caption_column,
    chunk_frame_embedding_column,
    embed_caption_column,
    embed_frame_column,
    embed_text_column,
    summary_column,
)


if TYPE_CHECKING:
    import lancedb

logger = logging.getLogger(__name__)

# Re-exported so ``from ratch.features.columns import ...`` keeps resolving the
# builders/constants that now live in ``embed_columns``.
__all__ = [
    "CAPTION_COLUMN",
    "CAPTION_EMBED_COLUMN",
    "CHUNK_KEYS",
    "FEATURES",
    "FRAME_EMBED_COLUMN",
    "FRAME_KEYS",
    "SUMMARY_COLUMN",
    "TEXT_EMBED_COLUMN",
    "VECTOR_TYPE",
    "Feature",
    "FeatureRunOptions",
    "caption_column",
    "chunk_frame_embedding_column",
    "embed_caption_column",
    "embed_frame_column",
    "embed_text_column",
    "summary_column",
]


# ─────────────────────────────── Registry ───────────────────────────────────


class FeatureRunOptions(BaseModel):
    """Knobs the ``ratch feature <name>`` CLI passes into a feature's ``run``."""

    url: str | None = None  # model server base URL; None → the feature's own default
    # Generative-feature overrides (caption): None → the client's own default
    # (Gemma 4 / the Swedish instruction). Ignored by the embedding features.
    model: str | None = None
    instruction: str | None = None
    batch_rows: int = 256
    overwrite: bool = False  # --all: drop and rebuild rather than fill-NULL
    create_index: bool = True  # vector features only
    num_partitions: int = 256
    num_sub_vectors: int = 64
    checkpoint: Path | None = None
    # Atlas projection space — routes the 'atlas' feature, ignored by the rest:
    # 'text' (text_embedding → atlas_*), 'visual' (chunk frame_embedding →
    # atlas_img_*), or 'caption' (chunk caption_embedding → atlas_cap_*).
    space: Literal["text", "visual", "caption"] = "text"


class Feature(BaseModel):
    """One derived column: where it lives + how to build it."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    table: str  # "chunks" or "chunk_frames"
    description: str
    run: Callable[[Path, FeatureRunOptions, Callable[[int], None] | None], int]


def _open_table(db_path: Path, table: str) -> lancedb.table.Table:
    import lancedb

    return lancedb.connect(str(db_path)).open_table(table)


# Builder for an embedding feature: (table file, builder fn, vector column).
EmbedBuilder = Callable[..., int]


def _run_embedding_feature(
    db_path: Path,
    opts: FeatureRunOptions,
    progress: Callable[[int], None] | None,
    *,
    table: str,
    column: str,
    build: EmbedBuilder,
) -> int:
    """Shared body for every ``VLLMEmbeddingClient`` feature.

    Builds the production embedding client (``opts.url`` or the package default),
    calls the per-feature column ``build`` (which writes ``column`` to the
    ``{table}.lance`` file), then — when ``opts.create_index`` — attaches the
    vector index. The index/overwrite/``--only-null``/checkpoint wiring is
    single-sourced here so the text/frame/caption runners can't drift apart.
    """
    from ratch.clients.embedding import DEFAULT_EMBED_URL, VLLMEmbeddingClient

    client = VLLMEmbeddingClient(opts.url or DEFAULT_EMBED_URL)
    n = build(
        db_path / f"{table}.lance",
        client=client,
        batch_rows=opts.batch_rows,
        checkpoint_file=opts.checkpoint,
        overwrite=opts.overwrite,
        progress=progress,
    )
    if opts.create_index:
        ensure_vector_index(
            _open_table(db_path, table),
            column,
            num_partitions=opts.num_partitions,
            num_sub_vectors=opts.num_sub_vectors,
        )
    return n


def _run_text_embedding(db_path: Path, opts: FeatureRunOptions, progress: Callable[[int], None] | None) -> int:
    return _run_embedding_feature(
        db_path,
        opts,
        progress,
        table="chunks",
        column=TEXT_EMBED_COLUMN,
        build=embed_text_column,
    )


def _run_frame_embedding(db_path: Path, opts: FeatureRunOptions, progress: Callable[[int], None] | None) -> int:
    return _run_embedding_feature(
        db_path,
        opts,
        progress,
        table="chunk_frames",
        column=FRAME_EMBED_COLUMN,
        build=embed_frame_column,
    )


def _run_caption_embedding(db_path: Path, opts: FeatureRunOptions, progress: Callable[[int], None] | None) -> int:
    return _run_embedding_feature(
        db_path,
        opts,
        progress,
        table="chunk_frames",
        column=CAPTION_EMBED_COLUMN,
        build=embed_caption_column,
    )


def _run_summary(db_path: Path, opts: FeatureRunOptions, progress: Callable[[int], None] | None) -> int:
    from ratch.clients.summarize import DEFAULT_SUMMARIZE_URL, VLLMSummarizeClient

    client = VLLMSummarizeClient(opts.url or DEFAULT_SUMMARIZE_URL)
    return summary_column(
        db_path / "chunks.lance",
        client=client,
        batch_rows=opts.batch_rows,
        checkpoint_file=opts.checkpoint,
        overwrite=opts.overwrite,
        progress=progress,
    )


def _run_caption(db_path: Path, opts: FeatureRunOptions, progress: Callable[[int], None] | None) -> int:
    from ratch.clients.caption import (
        CAPTION_INSTRUCTION,
        CAPTION_MODEL,
        DEFAULT_CAPTION_URL,
        VLLMCaptionClient,
    )

    # model/instruction fall back to the client's own defaults (Gemma 4 / Swedish)
    # when the CLI didn't override them.
    client = VLLMCaptionClient(
        opts.url or DEFAULT_CAPTION_URL,
        model=opts.model or CAPTION_MODEL,
        instruction=opts.instruction or CAPTION_INSTRUCTION,
    )
    n = caption_column(
        db_path / "chunk_frames.lance",
        client=client,
        batch_rows=opts.batch_rows,
        checkpoint_file=opts.checkpoint,
        overwrite=opts.overwrite,
        progress=progress,
    )
    # Tantivy FTS index so captions are keyword-searchable (mode=scene, keyword).
    if opts.create_index:
        ensure_fts_index(_open_table(db_path, "chunk_frames"), CAPTION_COLUMN)
    return n


def _run_atlas(db_path: Path, opts: FeatureRunOptions, progress: Callable[[int], None] | None) -> int:
    """Text-space atlas, or route to the visual/caption path per ``--space``."""
    if opts.space == "visual":
        return _run_atlas_visual(db_path, opts, progress)
    if opts.space == "caption":
        return _run_atlas_caption(db_path, opts, progress)
    from .projection import project_atlas_columns

    return project_atlas_columns(
        db_path / "chunks.lance",
        overwrite=opts.overwrite,
        batch_rows=opts.batch_rows,
        progress=progress,
    )


def _run_atlas_visual(db_path: Path, opts: FeatureRunOptions, progress: Callable[[int], None] | None) -> int:
    """Visual-space atlas: chunk-level ``frame_embedding`` → ``atlas_img_*``.

    Two pure-Lance steps, no GPU/re-embedding: (a) ensure a chunk-level
    ``frame_embedding`` exists on ``chunks`` (joined from ``chunk_frames``'
    representative ``frame_idx=0`` vector), then (b) project it into the
    namespaced ``atlas_img_x/atlas_img_y/atlas_img_cluster`` triplet.
    """
    from .projection import project_atlas_columns

    chunks_path = db_path / "chunks.lance"
    frames_path = db_path / "chunk_frames.lance"
    if not frames_path.exists():
        raise ValueError("chunk_frames table missing — run `ratch extract-chunk-frames` and `ratch feature frame_embedding` before the visual atlas.")
    chunk_frame_embedding_column(
        chunks_path,
        frames_path,
        batch_rows=opts.batch_rows,
        overwrite=opts.overwrite,
    )
    return project_atlas_columns(
        chunks_path,
        source_column=FRAME_EMBED_COLUMN,
        column_prefix="atlas_img_",
        overwrite=opts.overwrite,
        batch_rows=opts.batch_rows,
        progress=progress,
    )


def _run_atlas_caption(db_path: Path, opts: FeatureRunOptions, progress: Callable[[int], None] | None) -> int:
    """Caption-space atlas: chunk-level ``caption_embedding`` → ``atlas_cap_*``.

    Mirrors :func:`_run_atlas_visual` but over the frame's Swedish-caption text
    vector instead of the raw image vector: (a) join each chunk's representative
    (``frame_idx=0``) ``caption_embedding`` from ``chunk_frames`` onto ``chunks``,
    then (b) project it into ``atlas_cap_x/atlas_cap_y/atlas_cap_cluster``. So the
    map clusters chunks by what their frame *depicts* (described in language),
    distinct from text (what's said) and visual (raw image similarity).
    """
    from .projection import project_atlas_columns

    chunks_path = db_path / "chunks.lance"
    frames_path = db_path / "chunk_frames.lance"
    if not frames_path.exists():
        raise ValueError("chunk_frames table missing — run `ratch feature caption` and `ratch feature caption_embedding` before the caption atlas.")
    chunk_frame_embedding_column(
        chunks_path,
        frames_path,
        column=CAPTION_EMBED_COLUMN,
        batch_rows=opts.batch_rows,
        overwrite=opts.overwrite,
    )
    return project_atlas_columns(
        chunks_path,
        source_column=CAPTION_EMBED_COLUMN,
        column_prefix="atlas_cap_",
        overwrite=opts.overwrite,
        batch_rows=opts.batch_rows,
        progress=progress,
    )


def _run_topics(db_path: Path, opts: FeatureRunOptions, _progress: Callable[[int], None] | None) -> int:
    """Toponymy topic layers — the topics RUNNER's worker, driven as a job.

    ratch never imports toponymy: ``runners/topics/`` owns the transformers<5
    env, and :func:`ratch.core.jobs.run_runner` submits its worker as a Ray Job
    (``RATCH_RAY_ENABLED=1``, the runner's env in the job's runtime_env) or runs
    it in-process. Local sealed-env convenience is ``make topics``. The worker
    writes the ``topic_l*``/``doc_topic`` columns; the nested hierarchy JSON is
    then derived here (pure ratch compute). ``opts.url``, if set, overrides the
    LLM (Gemma) endpoint.
    """
    from ratch.core.jobs import RunnerJob, run_runner

    # Resolved absolute, like every path handed to Ray (ray_av/RunnerContext):
    # a Ray Job's cwd is the working_dir SNAPSHOT (which excludes *.lance data),
    # so a relative --db can never be found there. Resolving also canonicalises
    # the idempotency token — one dataset, one submission id, however spelled.
    db = str(Path(db_path).resolve())
    llm_args = ("--llm-url", opts.url) if opts.url else ()
    run_runner(RunnerJob(runner="topics", entrypoint_args=("--db", db, *llm_args), token=db))
    return _run_topic_tree(db_path, opts, _progress)


def _run_topic_tree(db_path: Path, _opts: FeatureRunOptions, _progress: Callable[[int], None] | None) -> int:
    """The nested topic hierarchy (Tree treemap JSONB) from existing ``topic_l*``
    columns — pure ratch compute, split out so the sealed-env Make path
    (worker → ``ratch feature topic_tree``) reaches it without the model step."""
    from .topic_tree import build_topic_tree

    return build_topic_tree(db_path)


FEATURES: dict[str, Feature] = {
    "text_embedding": Feature(
        name="text_embedding",
        table="chunks",
        description="Qwen3-VL text embedding (2048-d) for semantic search.",
        run=_run_text_embedding,
    ),
    "frame_embedding": Feature(
        name="frame_embedding",
        table="chunk_frames",
        description="Qwen3-VL frame embedding (2048-d) for visual search.",
        run=_run_frame_embedding,
    ),
    "summary": Feature(
        name="summary",
        table="chunks",
        description="One-line LLM summary of each chunk's transcript.",
        run=_run_summary,
    ),
    "caption": Feature(
        name="caption",
        table="chunk_frames",
        description="Gemma 4 Swedish caption of each chunk's frame (one factual sentence).",
        run=_run_caption,
    ),
    "caption_embedding": Feature(
        name="caption_embedding",
        table="chunk_frames",
        description="Qwen3-VL text embedding (2048-d) of each frame's caption for scene search.",
        run=_run_caption_embedding,
    ),
    "atlas": Feature(
        name="atlas",
        table="chunks",
        description="EVōC 2-D layout + clusters (atlas_x/atlas_y/atlas_cluster) for the Atlas view. "
        "Use --space visual for the frame_embedding map (atlas_img_*).",
        run=_run_atlas,
    ),
    "atlas_visual": Feature(
        name="atlas_visual",
        table="chunks",
        description="Visual EVōC map from chunk-level frame_embedding (atlas_img_x/atlas_img_y/atlas_img_cluster). Same as `atlas --space visual`.",
        run=_run_atlas_visual,
    ),
    "atlas_caption": Feature(
        name="atlas_caption",
        table="chunks",
        description="Caption EVōC map from chunk-level caption_embedding (atlas_cap_x/atlas_cap_y/atlas_cap_cluster). Same as `atlas --space caption`.",
        run=_run_atlas_caption,
    ),
    "topics": Feature(
        name="topics",
        table="chunks",
        description="Toponymy Swedish topic layers (topic_l0…) named by Gemma 4 over the EVōC atlas "
        "map — the topics runner's worker as a Ray Job (or in-process), then the hierarchy tree.",
        run=_run_topics,
    ),
    "topic_tree": Feature(
        name="topic_tree",
        table="chunks",
        description="Nested topic hierarchy JSONB from existing topic_l* columns (pure compute — the follow-up step after the sealed-env topics worker).",
        run=_run_topic_tree,
    ),
}
