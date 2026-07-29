"""Ray Data actor factory for the diarize runner — the model side of the stage.

Lives in the runner (the model's home) per the runners/ architecture: ratch's
composition root resolves this module by convention (``Stage.runner="diarize"``
→ ``runners.diarize.actor``) and hands ``compute_factory`` to
``run_append_rows_stage`` → ``map_batches`` (one warm model per actor). Deps
resolve from THIS runner's env on the workers (per-stage ``runtime_env`` at
merge; the ``[models]`` extra on a local single-node run).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa
from ratch.core.dataset import empty_table
from ratch.model.schema import SPEAKER_TURNS_SCHEMA


if TYPE_CHECKING:
    from collections.abc import Callable

    from ratch.core.runners import RunnerContext

logger = logging.getLogger(__name__)

OUTPUT_SCHEMA = SPEAKER_TURNS_SCHEMA


def compute_factory(ctx: RunnerContext) -> Callable[[pa.Table], pa.Table]:
    from ratch.ingest.audio import resolve_source

    from runners.diarize.diarize import Diarizer

    diarizer = Diarizer()  # pyannote loads once per actor

    def compute(batch: pa.Table) -> pa.Table:
        tables: list[pa.Table] = []
        for doc_id, audio_path in zip(batch["doc_id"].to_pylist(), batch["audio_path"].to_pylist(), strict=True):
            try:
                source = resolve_source(audio_path, Path(ctx.audio_root))
                if source is None:
                    raise FileNotFoundError(f"{audio_path} not under {ctx.audio_root}")
                turns = diarizer.diarize(source)
            except Exception as exc:
                logger.warning("diarization failed for %s: %s", doc_id, exc)
                continue
            if not turns:
                continue
            tables.append(
                pa.table(
                    {
                        "doc_id": pa.array([doc_id] * len(turns), pa.string()),
                        "turn_id": pa.array(list(range(len(turns))), pa.int32()),
                        "speaker_label": pa.array([t.speaker_label for t in turns], pa.string()),
                        "start": pa.array([t.start for t in turns], pa.float32()),
                        "end": pa.array([t.end for t in turns], pa.float32()),
                    },
                    schema=OUTPUT_SCHEMA,
                )
            )
        return pa.concat_tables(tables) if tables else empty_table(OUTPUT_SCHEMA)

    return compute
