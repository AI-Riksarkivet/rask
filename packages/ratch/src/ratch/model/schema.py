"""PyArrow schemas for the Lance tables that `ratch` produces.

* :data:`CHUNK_SCHEMA` — one row per :class:`AudioChunk`. Holds the ``text``
  column the Tantivy FTS index is built on. The ``text_embedding`` vector is
  **not** declared here: ``embed-chunks`` attaches it post-ingest via
  ``dataset.add_columns(...)`` (Lance "data evolution"), so ingest never writes
  a placeholder all-null vector column. Joined to the source media by
  ``doc_id``. Per-chunk video frames live in :data:`CHUNK_FRAMES_SCHEMA`
  (a separate table), never on ``chunks``.

* :data:`DOC_SCHEMA` — one row per source media file. ``media_blob`` is a Lance
  **Blob V2 External** column: it stores the media **URI** (``file://`` /
  ``hf://`` / ``s3://``) — *not* the bytes — so the table stays a small portable
  catalog while the media lives wherever the URI points. ``thumbnail`` is a Blob
  V2 *Inline* column (small JPEG bytes). Read either with ``ds.take_blobs(...)``,
  which returns lazy, range-readable ``BlobFile`` handles.

* :data:`CHUNK_FRAMES_SCHEMA` — one row per chunk's representative frame
  (``frame_blob``, Blob V2 Inline). A separate append-only table; see its comment.

Any ``blob_field`` column requires ``data_storage_version="2.2"`` (see the
``*_STORAGE_VERSION`` constants below).
"""

from __future__ import annotations

from typing import Final

import pyarrow as pa
from lance import blob_field


#: Native embedding dimension of Qwen3-VL-Embedding-2B (text and image both
#: project to this hidden size). We store the FULL vector — no Matryoshka
#: truncation — so retrieval keeps the model's full fidelity. Changing this
#: needs a re-ingest: a fixed-size-list column dimension can't change in place.
EMBED_DIM: Final = 2048

word_struct: pa.DataType = pa.struct(
    [
        pa.field("text", pa.string()),
        pa.field("start", pa.float32()),
        pa.field("end", pa.float32()),
        pa.field("score", pa.float32()),
    ]
)

alignment_struct: pa.DataType = pa.struct(
    [
        pa.field("start", pa.float32()),
        pa.field("end", pa.float32()),
        pa.field("text", pa.string()),
        pa.field("duration", pa.float32()),
        pa.field("score", pa.float32()),
        pa.field("words", pa.list_(word_struct)),
    ]
)


# ───────────────────────────── Chunk-centric schema ─────────────────────────
# One row per AudioChunk. No audio bytes here — those live on the documents
# table, keyed by `doc_id`. This keeps chunk scans (FTS, metadata queries)
# cheap regardless of how much media is stored in the DB.

CHUNK_SCHEMA: pa.Schema = pa.schema(
    [
        # Identity
        pa.field("doc_id", pa.string(), nullable=False),
        pa.field("speech_id", pa.int32(), nullable=False),
        pa.field("chunk_id", pa.int32(), nullable=False),
        # Document-level context (denormalised for filter-free retrieval).
        pa.field("audio_path", pa.string(), nullable=False),
        pa.field("sample_rate", pa.int32()),
        pa.field("audio_duration", pa.float32()),
        # Riksarkivet archival metadata (populated from a video_batcher CSV
        # at ingest time, keyed by bildid = audio_path stem).
        pa.field("referenskod", pa.string()),
        pa.field("namn", pa.string()),
        pa.field("bildid", pa.string()),
        pa.field("extraid", pa.string()),
        # Chunk payload — `text` is what the FTS index is built on.
        pa.field("start", pa.float32(), nullable=False),
        pa.field("end", pa.float32(), nullable=False),
        pa.field("duration", pa.float32()),
        pa.field("text", pa.string(), nullable=False),
        pa.field("audio_frames", pa.int64()),
        pa.field("num_logits", pa.int64()),
        pa.field("language", pa.string()),
        pa.field("language_prob", pa.float32()),
        # Word-level alignments that fall inside this chunk's [start, end]
        # stored as Lance JSONB (`pa.json_()`). Writers pass JSON strings;
        # Lance encodes them as compact binary internally. Readers get the
        # JSON text back — no binding-specific nested-struct decode. Lets
        # us add scalar/FTS indexes on JSON paths later if we ever want to.
        # Schema of each element:
        #   {"start": float, "end": float, "text": str, "duration": float,
        #    "score": float, "words": [{"text": str, "start": float,
        #    "end": float, "score": float}, ...]}
        pa.field("alignments_json", pa.json_()),
        pa.field("metadata", pa.string()),
    ]
)
# `text_embedding` (FixedSizeList<float32, EMBED_DIM>) is intentionally absent:
# `embed-chunks` adds it after ingest via `dataset.add_columns(...)`, which
# writes one new column file alongside the existing data instead of rewriting
# fragments. See `ratch.features.columns.embed_text_column`.
#
# Per-chunk video frames (blob + frame_embedding) are NOT here either — they
# live in CHUNK_FRAMES_SCHEMA, a separate table keyed by the same (doc_id,
# speech_id, chunk_id). See that schema's comment for why.


#: Lance file format version for the `chunks` dataset. `chunks` no longer holds
#: Blob V2 columns (frames moved to `chunk_frames`); we keep 2.2 for consistency
#: with the other tables. ``Final`` keeps it a ``Literal["2.2"]`` so it satisfies
#: Lance's ``data_storage_version`` parameter type.
CHUNK_STORAGE_VERSION: Final = "2.2"


# ───────────────────────────── Document-centric schema ──────────────────────
# One row per source media file.
#
# `media_blob` is a Lance Blob V2 External column — it stores URIs (not
# bytes), and a Python reader uses `ds.take_blobs("media_blob", ...)` to
# transparently fetch + range-read the underlying objects.
#   file:///abs/path/input/T0001417_00001.mp4    ← local dev
#   hf://buckets/you/ratch-videos/T0001417_00001.mp4
#   s3://bucket/videos/T0001417_00001.mp4
# Writes use `blob_array([uri, …])`. Requires data_storage_version="2.2".

DOC_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("doc_id", pa.string(), nullable=False),
        pa.field("audio_path", pa.string(), nullable=False),
        pa.field("sample_rate", pa.int32()),
        pa.field("duration", pa.float32()),
        # Riksarkivet archival metadata (mirrored from the CSV at ingest).
        pa.field("referenskod", pa.string()),
        pa.field("namn", pa.string()),
        pa.field("bildid", pa.string()),
        pa.field("extraid", pa.string()),
        # ISO 639-1 language code for the whole document (sv, en, de, fr, …).
        # Populated at ingest time either from --doc-language or inferred from
        # the alignments directory name (e.g. output/sv/alignments → "sv").
        pa.field("language", pa.string()),
        pa.field("media_mime", pa.string()),
        # Lance Blob V2 External: `blob_array(["file://…", "s3://…", "hf://…"])`.
        # Lance returns file-like BlobFile handles via `ds.take_blobs(...)`.
        blob_field("media_blob", nullable=True),
        # Lance Blob V2 (Inline mode for small objects). JPEG thumbnail bytes
        # live inside the main data file; no sidecar. Read via `take_blobs`.
        blob_field("thumbnail", nullable=True),
        pa.field("thumbnail_mime", pa.string()),
        pa.field("metadata", pa.string()),
    ]
)


#: Lance file format version required for Blob V2.
DOC_STORAGE_VERSION: Final = "2.2"


# ─────────────────────── Chunk-frames table (Phase 2 v2) ────────────────────
# A NEW separate table, keyed by (doc_id, speech_id, chunk_id, frame_idx), built
# via append-only writes during `extract-chunk-frames`. ``frame_idx`` lets a
# chunk hold more than one frame (0 for the single representative frame; 0..K-1
# when sampling multiple per chunk). We keep it separate from
# `chunks` because Lance 4.0's `merge_insert` crashes on wide schemas with
# multiple extension types when filling blob columns post-hoc:
#   `Invalid user input: there were more fields in the schema than provided
#    column indices / infos` (decoder.rs:438) — confirmed at row counts 1,
#   100, and 145k. Lance docs explicitly recommend a separate table + append
#   for "data evolution"-style workloads (Lance file format 2.2 docs):
#   "Adding a column with backfilled data writes new data alongside existing
#    files; the original data is never touched."
#
# The frame_embedding column is added later via `dataset.add_columns(...)`
# from the embed-chunk-frames step — also append-only at the column level,
# bypasses the merge_insert join entirely.
CHUNK_FRAMES_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("doc_id", pa.string(), nullable=False),
        pa.field("speech_id", pa.int32(), nullable=False),
        pa.field("chunk_id", pa.int32(), nullable=False),
        # 0 for the single representative frame; 0..K-1 when a chunk is sampled
        # at multiple timestamps. Part of the row key.
        pa.field("frame_idx", pa.int32(), nullable=False),
        # ~50 KB JPEG → goes into Blob V2 Inline tier (≤64 KB threshold)
        # per the file format 2.2 spec. Read via `ds.take_blobs("frame_blob")`.
        blob_field("frame_blob", nullable=False),
        pa.field("frame_mime", pa.string(), nullable=False),
        pa.field("frame_width", pa.int32(), nullable=False),
        pa.field("frame_height", pa.int32(), nullable=False),
    ]
)


#: Lance file format version required for chunk_frames (blob_field).
CHUNK_FRAMES_STORAGE_VERSION: Final = "2.2"


# ───────────────────────── Speaker-turns table (diarization) ────────────────
# A NEW separate, append-only table holding pyannote speaker-diarization output:
# one row per diarized turn, keyed logically by (doc_id, turn_id). `turn_id` is
# the per-video enumerate index of the turn (turns sorted by `start`).
# `speaker_label` is pyannote's local label ("SPEAKER_00", "SPEAKER_01", …) —
# stable only *within* a single video, never across videos. `start`/`end` are
# ABSOLUTE video seconds. Built offline by `ratch extract-speaker-turns`; read
# on demand by the backend (`GET /api/diarization/{doc_id}`). Kept separate from
# `chunks` for the same reason `chunk_frames` is: avoid `merge_insert` against
# the wide `chunks` schema, and one video's turns are produced as a unit.
SPEAKER_TURNS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("doc_id", pa.string(), nullable=False),
        pa.field("turn_id", pa.int32(), nullable=False),
        pa.field("speaker_label", pa.string(), nullable=False),
        pa.field("start", pa.float32(), nullable=False),
        pa.field("end", pa.float32(), nullable=False),
    ]
)


#: Lance file format version for the speaker_turns dataset (kept at 2.2 for
#: consistency with the other tables; no Blob V2 columns here).
SPEAKER_TURNS_STORAGE_VERSION: Final = "2.2"


# ──────────────────── Speaker-embeddings table (voiceprints) ────────────────
# One row per *embedded* diarized turn: the `speaker_turns` rows that pass the
# `--min-turn-duration` gate, keyed logically by (doc_id, turn_id) — the same
# key as the matching speaker_turns row. `embedding` is the L2-normalized 256-d
# output of pyannote community-1's internal WeSpeaker-ResNet34 encoder (see
# `runners.voiceprint.voiceprint`; the raw model outputs are NOT unit-norm, the writer
# normalizes before storing so cosine kNN is well-defined). `speaker_label` /
# `start` / `end` / `duration` are denormalised from speaker_turns so a voice
# kNN hit resolves to its turn without a join. Built offline by
# `ratch embed-speaker-turns`; queried by the backend's voice-similarity kNN.

#: Output dimension of pyannote community-1's internal WeSpeaker-ResNet34
#: speaker-embedding model. Deliberately a separate constant from EMBED_DIM
#: (2048, Qwen3-VL): the `features.columns` vector helpers assert 2048 and must
#: never be reused for voice vectors.
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


#: Lance file format version for the speaker_embeddings dataset (kept at 2.2
#: for consistency with the other tables; no Blob V2 columns here).
SPEAKER_EMBEDDINGS_STORAGE_VERSION: Final = "2.2"


# ──────────────────── Speakers table (per-video voiceprints) ────────────────
# One row per (doc_id, speaker_label): the duration-weighted mean of that
# speaker's turn embeddings, re-L2-normalized — a single voiceprint per local
# diarization speaker. `speaker_cluster` defaults to -1; a later global
# clustering pass fills it to assign cross-video identities, and
# `speaker_name` stays NULL until that pass (or a human) names the cluster.
# Tiny (a few rows per video), so `ratch build-speakers` rebuilds it
# wholesale (overwrite) from `speaker_embeddings` each run.

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


#: Lance file format version for the speakers dataset (kept at 2.2 for
#: consistency with the other tables; no Blob V2 columns here).
SPEAKERS_STORAGE_VERSION: Final = "2.2"
