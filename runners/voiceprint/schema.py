"""PyArrow schemas for the two Lance tables this runner writes.

VENDORED from ratch at the dissolution (2026-08-28, ``open_ray-kernel.md``); origin
``packages/ratch/src/ratch/model/schema.py``, narrowed to what this runner declares. A COPY because
the runner is sealed (``requires-python >=3.10,<3.13``; platform packages are ``>=3.13``).

TAKEN: :data:`VOICE_EMBED_DIM`, :data:`SPEAKER_EMBEDDINGS_SCHEMA`, :data:`SPEAKERS_SCHEMA`.
Nothing they reference was left behind: neither
schema uses a ``blob_field``, so the origin's ``from lance import blob_field`` is not carried and
this module costs only pyarrow — which is why the call sites that defer importing it can keep
deferring (see ``voiceprint.py``: the Ray Serve replica must not pull Arrow in behind the encoder).
LEFT BEHIND: ``CHUNK_SCHEMA``, ``DOC_SCHEMA``, ``CHUNK_FRAMES_SCHEMA``, ``SPEAKER_TURNS_SCHEMA``,
``EMBED_DIM`` and the alignment structs — corpus-wide tables that are not this workload's output —
and the per-table ``*_STORAGE_VERSION`` constants, which no call site here reads (the storage
version that is actually APPLIED lives with the writer, ``dataset.DATA_STORAGE_VERSION``).
(``speaker_turns`` is READ actor-side by column name, never written here, so its schema is not
needed; the diarize runner owns it.)
"""

from __future__ import annotations

from typing import Final

import pyarrow as pa


# ──────────────────── Speaker-embeddings table (voiceprints) ────────────────
# One row per *embedded* diarized turn: the `speaker_turns` rows that pass the
# min-turn-duration gate, keyed logically by (doc_id, turn_id) — the same
# key as the matching speaker_turns row. `embedding` is the L2-normalized 256-d
# output of pyannote community-1's internal WeSpeaker-ResNet34 encoder (see
# `runners.voiceprint.voiceprint`; the raw model outputs are NOT unit-norm, the writer
# normalizes before storing so cosine kNN is well-defined). `speaker_label` /
# `start` / `end` / `duration` are denormalised from speaker_turns so a voice
# kNN hit resolves to its turn without a join. Built offline by this runner;
# queried by the backend's voice-similarity kNN.

#: Output dimension of pyannote community-1's internal WeSpeaker-ResNet34
#: speaker-embedding model. Deliberately a separate constant from the corpus
#: text/image embedding dimension (2048, Qwen3-VL): the shared vector helpers
#: assert 2048 and must never be reused for voice vectors.
VOICE_EMBED_DIM: Final = 256

SPEAKER_EMBEDDINGS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("doc_id", pa.string(), nullable=False),
        pa.field("turn_id", pa.int32(), nullable=False),
        pa.field("speaker_label", pa.string(), nullable=False),
        pa.field("start", pa.float32()),
        pa.field("end", pa.float32()),
        pa.field("duration", pa.float32()),
        pa.field("embedding", pa.list_(pa.float32(), VOICE_EMBED_DIM)),
    ]
)


# ──────────────────── Speakers table (per-video voiceprints) ────────────────
# One row per (doc_id, speaker_label): the duration-weighted mean of that
# speaker's turn embeddings, re-L2-normalized — a single voiceprint per local
# diarization speaker. `speaker_cluster` defaults to -1; a later global
# clustering pass fills it to assign cross-video identities, and
# `speaker_name` stays NULL until that pass (or a human) names the cluster.
# Tiny (a few rows per video), so `build_speakers` rebuilds it wholesale
# (overwrite) from `speaker_embeddings` each run.

SPEAKERS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("doc_id", pa.string(), nullable=False),
        pa.field("speaker_label", pa.string(), nullable=False),
        pa.field("n_turns", pa.int32()),
        pa.field("total_duration", pa.float32()),
        pa.field("embedding", pa.list_(pa.float32(), VOICE_EMBED_DIM)),
        pa.field("speaker_cluster", pa.int32()),
        pa.field("speaker_name", pa.string()),
    ]
)
