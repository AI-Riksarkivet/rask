"""The PyArrow schema of the ``speaker_turns`` Lance table this runner writes.

Vendored from ``ratch.model.schema`` at the ratch dissolution (2026-08-28 —
``open_ray-kernel.md``, move 10: "Push ``SPEAKER_TURNS``/``EMBEDDINGS``/
``SPEAKERS_SCHEMA`` down to their runners"). It is a COPY rather than an import,
and that is the seal working rather than drift: this runner is SEALED — its own
``pyproject.toml`` pins ``requires-python = ">=3.10,<3.13"`` for the cu128 torch
stack, while every platform package (``ratch``, ``service-kit``, ``ray-kit``) is
``>=3.13``. No platform package can be imported here at all.

Moving it down is also the right home independent of the version wall: a
workload's output shape belongs to the workload, and a per-workload schema
sitting in a shared package is precisely what got ``medallion/schemas/htr.py``
deleted on 2026-08-17.

Only ``SPEAKER_TURNS_SCHEMA`` travels. The origin's ``CHUNK_*``, ``DOC_*``,
``CHUNK_FRAMES_*``, ``SPEAKER_EMBEDDINGS_*`` and ``SPEAKERS_*`` schemas are
other workloads' business (``voiceprint`` owns the last two) or the platform's.
The origin's companion constant ``SPEAKER_TURNS_STORAGE_VERSION = "2.2"`` is
deliberately NOT copied: nothing here ever read it, and the storage version that
actually governs these writes is :data:`dataset.DATA_STORAGE_VERSION` — carried
next to the write calls it constrains, where it cannot be forgotten.
"""

from __future__ import annotations

import pyarrow as pa


# ───────────────────────── Speaker-turns table (diarization) ────────────────
# A separate, append-only table holding pyannote speaker-diarization output:
# one row per diarized turn, keyed logically by (doc_id, turn_id). `turn_id` is
# the per-video enumerate index of the turn (turns sorted by `start`).
# `speaker_label` is pyannote's local label ("SPEAKER_00", "SPEAKER_01", …) —
# stable only *within* a single video, never across videos. `start`/`end` are
# ABSOLUTE video seconds. Built offline by this runner; read on demand by the
# backend. Kept separate from a wide chunk-centric table on purpose: avoid
# `merge_insert` against a wide schema, and one video's turns are produced as a
# unit.
SPEAKER_TURNS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("doc_id", pa.string(), nullable=False),
        pa.field("turn_id", pa.int32(), nullable=False),
        pa.field("speaker_label", pa.string(), nullable=False),
        pa.field("start", pa.float32(), nullable=False),
        pa.field("end", pa.float32(), nullable=False),
    ]
)
