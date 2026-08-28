"""Ray Data actor factory for the voiceprint runner — the model side of the stage.

Lives in the runner (the model's home) per the runners/ architecture: a driver resolves
this module by the stage↔runner convention (``Stage.runner="voiceprint"`` →
``runners.voiceprint.actor``) and hands ``compute_factory`` to ``map_batches``
(one warm model per actor), so the two exports below — ``compute_factory`` and
``OUTPUT_SCHEMA`` — are the whole coupling. Deps resolve from THIS runner's env
on the workers, out of a prebuilt image (``runtime_env.image_uri``, the
2026-08-25 baked-image ruling); ratch's ``runner_env()``, which instead pip-installed
this pyproject's unlocked deps per stage, is retired with ratch itself.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa

from runners.voiceprint.dataset import empty_table
from runners.voiceprint.schema import SPEAKER_EMBEDDINGS_SCHEMA


if TYPE_CHECKING:
    from collections.abc import Callable

    from runners.voiceprint.context import RunnerContext

logger = logging.getLogger(__name__)

OUTPUT_SCHEMA = SPEAKER_EMBEDDINGS_SCHEMA


def compute_factory(ctx: RunnerContext) -> Callable[[pa.Table], pa.Table]:
    """Doc-level on purpose: WeSpeaker output depends on the duration-sorted
    padding groups inside ``embed_turns``, so each document's turns are read
    (from the diarize stage's output table, actor-side) and embedded together —
    bit-identical to the engine path."""
    import lance
    import numpy as np

    from runners.voiceprint.audio import resolve_source
    from runners.voiceprint.schema import VOICE_EMBED_DIM
    from runners.voiceprint.voiceprint import TurnSpan, VoiceEncoder
    from runners.voiceprint.wav import extract_wav_16k_mono

    encoder = VoiceEncoder()  # WeSpeaker loads once per actor
    # Voiceprint's input dependency is diarize's output — the runner knows its
    # own upstream table, the driver doesn't have to.
    turns_uri = str((Path(ctx.db_path) / "speaker_turns.lance").resolve())
    turns_ds = lance.dataset(turns_uri)

    def compute(batch: pa.Table) -> pa.Table:
        tables: list[pa.Table] = []
        for doc_id, audio_path in zip(batch["doc_id"].to_pylist(), batch["audio_path"].to_pylist(), strict=True):
            rows = turns_ds.to_table(
                columns=["turn_id", "speaker_label", "start", "end"],
                filter=f"doc_id = '{doc_id}'",
            )
            turns = [
                TurnSpan(turn_id=int(t), speaker_label=label, start=float(s), end=float(e))
                for t, label, s, e in zip(
                    rows["turn_id"].to_pylist(),
                    rows["speaker_label"].to_pylist(),
                    rows["start"].to_pylist(),
                    rows["end"].to_pylist(),
                    strict=True,
                )
            ]
            if not turns:
                continue
            try:
                source = resolve_source(audio_path, Path(ctx.audio_root))
                if source is None:
                    raise FileNotFoundError(f"{audio_path} not under {ctx.audio_root}")
                with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
                    extract_wav_16k_mono(source, Path(tmp.name))
                    kept, embeddings = encoder.embed_turns(Path(tmp.name), turns)
            except Exception as exc:
                logger.warning("voiceprint failed for %s: %s", doc_id, exc)
                continue
            if not kept:
                continue
            flat = pa.array(embeddings.astype(np.float32).reshape(-1), pa.float32())
            tables.append(
                pa.table(
                    {
                        "doc_id": pa.array([doc_id] * len(kept), pa.string()),
                        "turn_id": pa.array([t.turn_id for t in kept], pa.int32()),
                        "speaker_label": pa.array([t.speaker_label for t in kept], pa.string()),
                        "start": pa.array([t.start for t in kept], pa.float32()),
                        "end": pa.array([t.end for t in kept], pa.float32()),
                        "duration": pa.array([t.duration for t in kept], pa.float32()),
                        "embedding": pa.FixedSizeListArray.from_arrays(flat, VOICE_EMBED_DIM),
                    },
                    schema=OUTPUT_SCHEMA,
                )
            )
        return pa.concat_tables(tables) if tables else empty_table(OUTPUT_SCHEMA)

    return compute
