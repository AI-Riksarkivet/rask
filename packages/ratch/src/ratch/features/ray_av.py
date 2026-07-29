"""Ray composition root for the AV append stages (frames / diarize / voiceprint).

Pure orchestration: MODEL actor factories live in their runners and are resolved
by convention (``Stage.runner`` → ``runners.<name>.actor.compute_factory``, one
warm model per Ray actor) — this module never names a model. Only the model-free
``frames`` factory (ffmpeg) is composed here. Media bytes
are read from the filesystem inside the actor — per LANCE_MEDIA_MERGE §4.3 only
the small frame JPEGs ride back through Ray Data blocks.

Per-item failures warn and skip (one bad video never kills a batch); loud
failures stay reserved for correctness bugs.
"""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa

from ratch.core.dataset import create_dataset, empty_table
from ratch.core.driver import run_append_rows_stage
from ratch.core.registry import Stage
from ratch.errors import RatchError


if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Frame sampled this far into a chunk — matches the old extract-chunk-frames
#: policy (the representative frame is the chunk's start).
FRAME_AT_CHUNK_START_S = 0.0


def frames_compute(audio_root: str) -> Callable[[pa.Table], pa.Table]:
    from lance import blob_array

    from ratch.ingest.audio import resolve_source
    from ratch.modalities.av.frames import extract_chunk_frame
    from ratch.model.schema import CHUNK_FRAMES_SCHEMA

    def compute(batch: pa.Table) -> pa.Table:
        rows: list[tuple[str, int, int, bytes, int, int]] = []
        for doc_id, speech_id, chunk_id, start, audio_path in zip(
            batch["doc_id"].to_pylist(),
            batch["speech_id"].to_pylist(),
            batch["chunk_id"].to_pylist(),
            batch["start"].to_pylist(),
            batch["audio_path"].to_pylist(),
            strict=True,
        ):
            try:
                source = resolve_source(audio_path, Path(audio_root))
                if source is None:
                    raise FileNotFoundError(f"{audio_path} not under {audio_root}")
                jpeg, width, height = extract_chunk_frame(source=source, time_sec=float(start) + FRAME_AT_CHUNK_START_S)
                rows.append((doc_id, int(speech_id), int(chunk_id), jpeg, width, height))
            except Exception as exc:
                logger.warning("frame extraction failed for %s/%s/%s: %s", doc_id, speech_id, chunk_id, exc)
        if not rows:
            return empty_table(CHUNK_FRAMES_SCHEMA)
        return pa.table(
            {
                "doc_id": pa.array([r[0] for r in rows], pa.string()),
                "speech_id": pa.array([r[1] for r in rows], pa.int32()),
                "chunk_id": pa.array([r[2] for r in rows], pa.int32()),
                "frame_idx": pa.array([0] * len(rows), pa.int32()),
                "frame_blob": blob_array([r[3] for r in rows]),
                "frame_mime": pa.array(["image/jpeg"] * len(rows), pa.string()),
                "frame_width": pa.array([r[4] for r in rows], pa.int32()),
                "frame_height": pa.array([r[5] for r in rows], pa.int32()),
            },
            schema=CHUNK_FRAMES_SCHEMA,
        )

    return compute


def run_append_stage(db_path: str | Path, stage: Stage, *, audio_root: str = "input/sv") -> int:
    """Dispatch an APPEND_ROWS stage to the Ray driver.

    Runner-backed stages resolve by convention — ``runners.<stage.runner>.actor``
    exports ``compute_factory`` + ``OUTPUT_SCHEMA`` — so a new model is one
    runner dir + one Stage entry, zero edits here. Only the pure-compute frames
    binding (ffmpeg, no model) is composed in this module.
    """
    from ratch.core.runners import RunnerContext, resolve_runner_actor
    from ratch.model.schema import CHUNK_FRAMES_SCHEMA

    # Absolute paths throughout: a relative path would resolve against the Ray
    # workers' runtime-env working-dir copy, failing every per-item read.
    context = RunnerContext(db_path=str(Path(db_path).resolve()), audio_root=str(Path(audio_root).resolve()))

    factory: Callable[[], Callable[[pa.Table], pa.Table]]
    if stage.runner is not None:
        actor = resolve_runner_actor(stage.runner)
        factory = partial(actor.compute_factory, context)
        output_schema: pa.Schema = actor.OUTPUT_SCHEMA
    elif stage.name == "extract_frames":
        factory = partial(frames_compute, context.audio_root)
        output_schema = CHUNK_FRAMES_SCHEMA
    else:
        raise RatchError(f"no binding for append stage {stage.name!r} — model stages declare Stage.runner; pure-compute stages are composed in ray_av")

    out_uri = str(Path(db_path) / f"{stage.output_table}.lance")
    return run_append_rows_stage(
        db_path,
        stage,
        factory=factory,
        output_schema=output_schema,
        create_output=lambda: create_dataset(out_uri, output_schema),
    )
