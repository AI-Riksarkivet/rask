"""The corpus's concrete stage declarations — composition-root DATA, not core code.

The core (:mod:`ratch.core.registry`) defines what a Stage IS; this module
declares which stages THIS corpus runs, including its column names
(``audio_path`` etc.) — exactly the knowledge the dataset descriptor carries,
restated for the pipeline. A different corpus supplies a different module (or,
eventually, a descriptor-derived one) without touching the core.
"""

from __future__ import annotations

from ratch.core.registry import AUDIO_VIDEO, VIDEO_ONLY, ActorConfig, Stage, StageShape


# Keys mirror ratch.features.embed_columns CHUNK_KEYS/FRAME_KEYS.
_CHUNK_KEYS = ("doc_id", "speech_id", "chunk_id")
_FRAME_KEYS = (*_CHUNK_KEYS, "frame_idx")

STAGES: dict[str, Stage] = {
    stage.name: stage
    for stage in (
        Stage(
            name="text_embedding",
            shape=StageShape.SCAN_COLUMN,
            table="chunks",
            read_columns=("text",),
            key_columns=_CHUNK_KEYS,
            output_columns=("text_embedding",),
            client="embed",
            actor=ActorConfig(max_actors=4, batch_rows=256),
        ),
        Stage(
            name="summary",
            shape=StageShape.SCAN_COLUMN,
            table="chunks",
            read_columns=("text",),
            key_columns=_CHUNK_KEYS,
            output_columns=("summary",),
            client="summarize",
            actor=ActorConfig(max_actors=2, batch_rows=128),
        ),
        Stage(
            name="frame_embedding",
            shape=StageShape.BLOB_COLUMN,
            table="chunk_frames",
            blob_column="frame_blob",
            key_columns=_FRAME_KEYS,
            output_columns=("frame_embedding",),
            client="embed",
            actor=ActorConfig(max_actors=2, batch_rows=64),
        ),
        Stage(
            name="caption",
            shape=StageShape.BLOB_COLUMN,
            table="chunk_frames",
            blob_column="frame_blob",
            key_columns=_FRAME_KEYS,
            output_columns=("caption",),
            client="caption",
            actor=ActorConfig(max_actors=2, batch_rows=32),
        ),
        Stage(
            name="caption_embedding",
            shape=StageShape.SCAN_COLUMN,
            table="chunk_frames",
            read_columns=("caption",),
            key_columns=_FRAME_KEYS,
            output_columns=("caption_embedding",),
            client="embed",
            actor=ActorConfig(max_actors=4, batch_rows=256),
        ),
        Stage(
            name="extract_frames",
            shape=StageShape.APPEND_ROWS,
            table="chunks",
            read_columns=("doc_id", "speech_id", "chunk_id", "start", "audio_path"),
            key_columns=_CHUNK_KEYS,
            output_table="chunk_frames",
            media_gate=VIDEO_ONLY,
            client="frames",
            actor=ActorConfig(max_actors=4, num_cpus=2.0, batch_rows=32),
        ),
        Stage(
            name="diarize",
            runner="diarize",
            shape=StageShape.APPEND_ROWS,
            table="documents",
            read_columns=("doc_id", "audio_path"),
            key_columns=("doc_id",),
            output_table="speaker_turns",
            media_gate=AUDIO_VIDEO,
            client="diarizer",
            actor=ActorConfig(max_actors=2, num_cpus=4.0, batch_rows=1),
        ),
        # Doc-level (one document per batch), NOT turn-level: WeSpeaker embeddings
        # depend on the duration-sorted padding groups inside embed_turns, so a
        # document's turns must be embedded together to reproduce the engine
        # path bit-for-bit. The actor reads the doc's turns itself.
        Stage(
            name="voiceprint",
            runner="voiceprint",
            shape=StageShape.APPEND_ROWS,
            table="documents",
            read_columns=("doc_id", "audio_path"),
            key_columns=("doc_id",),
            output_table="speaker_embeddings",
            media_gate=AUDIO_VIDEO,
            client="voice_encoder",
            actor=ActorConfig(max_actors=2, num_cpus=4.0, batch_rows=1),
        ),
    )
}
