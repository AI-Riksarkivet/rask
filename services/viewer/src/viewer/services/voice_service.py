"""Descriptor-driven voice-similarity retrieval — anchor resolve → kNN → chunk join.

Port of the old ``backend/voice/service.py`` for the media_api group: the
retrieval logic is unchanged, but every table/column that used to be hardcoded
is now resolved from the request's :class:`~service_kit.lancekit.registry.DatasetHandle`
descriptor — the voice/speakers capability targets, the row table, the identity
key fields, and the time axis. The hit post-processing (empty ``alignments`` +
representative-frame caption attach) is vendored here rather than imported from
the search group (LANCE_MEDIA_MERGE §4.4: the two router groups must not import
each other).

``similar_voices`` answers "where else does this voice speak?": it reads the
anchor voiceprint **from Lance** (no encoder runs at query time), kNN-ranks the
per-turn voice-embeddings table by cosine distance, and joins each matched turn
back to its max-overlap row-table chunk. ``similar_voices_for_upload`` is the
Lance-free anchor form: an uploaded snippet is ffmpeg-decoded and embedded
in-process on the CPU, then ranked through the same path so upload hits are
shaped exactly like the GET's. A cross-encoder rerank is deliberately NOT
supported — it reads transcript text, which is meaningless for voice identity.
"""

from __future__ import annotations

import logging
import math
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from pydantic import BaseModel

from service_kit.exceptions import NotFoundError, ServiceUnavailableError, ValidationError
from service_kit.lancekit import store
from service_kit.lancekit.predicate import and_, eq, isin, ne
from viewer.schemas.voice import (
    VoiceAnchor,
    VoiceIdentityAppearance,
    VoiceIdentityResponse,
    VoiceSimilarResponse,
    VoiceStatusResponse,
)
from viewer.services.wespeaker import (
    MIN_TURN_DURATION_S,
    TARGET_SAMPLE_RATE,
    extract_wav_16k_mono,
    l2_normalize,
    load_wav_16k_mono,
)


if TYPE_CHECKING:
    from collections.abc import Callable

    from service_kit.lancekit.descriptor import Declared
    from service_kit.lancekit.registry import DatasetHandle
    from viewer.services.wespeaker import TurnBatchEncoder

logger = logging.getLogger(__name__)

#: Hard cap on the result count — a turn→chunk join runs per hit.
_MAX_N = 100

#: Hard cap on an uploaded snippet's size — decode + embed run in-process.
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

#: Embed at most the first this-many seconds of an upload — the encoder was
#: trained on 5 s chunks, so longer audio adds CPU cost, not signal.
_UPLOAD_EMBED_CAP_S = 30.0

#: ffmpeg timeout for a ≤25 MB upload (the pipeline's 1800 s default is sized
#: for full-length videos, not snippets).
_UPLOAD_FFMPEG_TIMEOUT_S = 60.0

# IVF_PQ recall knobs — algorithmic constants (not corpus schema), duplicated
# from the old search constants because media_api may not import the search
# group. Ignored by Lance when the column has no IVF index.
_VECTOR_NPROBES = 20
_VECTOR_MAX_NPROBES = 0
_VECTOR_REFINE_FACTOR = 3

# The voice tables' own column names are the voice/speakers capability's
# writer-guaranteed contract (the embed-speaker-turns / build-speakers writers
# always produce exactly these), NOT corpus schema — so they stay module
# constants instead of descriptor lookups.
_TURN_DOC = "doc_id"
_TURN_ID = "turn_id"
_TURN_SPEAKER = "speaker_label"
_TURN_START = "start"
_TURN_END = "end"
_SPEAKER_N_TURNS = "n_turns"
_SPEAKER_TOTAL_DURATION = "total_duration"
_SPEAKER_CLUSTER = "speaker_cluster"
_DEFAULT_EMBEDDING_COLUMN = "embedding"

#: Voice-table columns a kNN hit needs (the join + voice fields).
_TURN_HIT_COLUMNS = [_TURN_DOC, _TURN_ID, _TURN_SPEAKER, _TURN_START, _TURN_END]

#: Speakers columns an identity appearance carries (the cluster id is read
#: separately — it may be absent on a pre-clustering table).
_IDENTITY_COLUMNS = [_TURN_DOC, _TURN_SPEAKER, _SPEAKER_N_TURNS, _SPEAKER_TOTAL_DURATION]

# Frames-capability writer contract: the representative caption frame is index 0.
_FRAME_IDX = "frame_idx"


class VoiceBindings(BaseModel):
    """The descriptor-resolved names one voice request works against."""

    embeddings_table: str
    embedding_column: str
    speakers_table: str | None
    row_table: str
    doc_key: str
    key_fields: list[str]
    time_start: str
    time_end: str
    payload_columns: list[str]
    captions_table: str | None
    captions_column: str | None


def _capability_table(declared: Declared, name: str) -> str | None:
    target = declared.capabilities.get(name)
    return target.partition(".")[0] if target else None


def _table_exists(handle: DatasetHandle, name: str | None) -> bool:
    # Probed on the store (not via the descriptor's startup snapshot) so a table
    # built after the app started is picked up without a restart — over S3 or local.
    return name is not None and store.exists(handle.table_uri(name), handle.storage_options)


def _payload_columns(handle: DatasetHandle, declared: Declared) -> list[str]:
    """The row-table projection a hit carries — declared roles ∩ live columns.

    Ordered, deduped union of identity keys, the time axis, and the display /
    filterable declarations. Vector, blob, and alignment columns can never
    appear (they are not declared roles), mirroring the old fixed payload list
    without naming any corpus column here.
    """
    if declared.search is None:
        return []
    wanted = [*declared.identity.key_fields]
    if declared.time is not None:
        # ``duration`` is a reserved convenience column (like the search group's
        # hit projection) — carried when the row table has it so voice hits keep
        # the field the frontend has always shown; never a declared corpus name.
        wanted += [declared.time.start, declared.time.end, "duration"]
    wanted += declared.display.title
    if declared.display.body:
        wanted.append(declared.display.body)
    wanted += [m.field for m in declared.display.metadata]
    wanted += declared.search.filterable
    info = handle.descriptor.tables.get(declared.search.row_table)
    live = {c.name for c in info.columns} if info is not None else set()
    out: list[str] = []
    for name in wanted:
        if name in live and name not in out:
            out.append(name)
    return out


def resolve_bindings(handle: DatasetHandle) -> VoiceBindings | None:
    """Resolve the voice capability against ``handle``; ``None`` = not available.

    Requires the voice capability to be declared, its embeddings table to exist
    on disk, and a declared search row table + time axis (the turn→chunk join
    is undefined without them).
    """
    declared = handle.descriptor.declared
    target = declared.capabilities.get("voice")
    if target is None:
        return None
    table, _, column = target.partition(".")
    if not _table_exists(handle, table):
        return None
    if declared.search is None or declared.time is None:
        logger.warning("voice capability declared but search/time bindings missing")
        return None

    speakers_table = _capability_table(declared, "speakers")
    captions_table: str | None = None
    captions_column: str | None = None
    captions_target = declared.capabilities.get("captions")
    if captions_target:
        captions_table, _, captions_column = captions_target.partition(".")
    return VoiceBindings(
        embeddings_table=table,
        embedding_column=column or _DEFAULT_EMBEDDING_COLUMN,
        speakers_table=speakers_table if _table_exists(handle, speakers_table) else None,
        row_table=declared.search.row_table,
        doc_key=declared.identity.doc_key,
        key_fields=declared.identity.key_fields,
        time_start=declared.time.start,
        time_end=declared.time.end,
        payload_columns=_payload_columns(handle, declared),
        captions_table=captions_table if captions_column else None,
        captions_column=captions_column or None,
    )


def _require_bindings(handle: DatasetHandle) -> VoiceBindings:
    bindings = resolve_bindings(handle)
    if bindings is None:
        raise ServiceUnavailableError("voice embeddings not built yet — run `ratch embed-speaker-turns`")
    return bindings


def voice_status(handle: DatasetHandle) -> VoiceStatusResponse:
    """Whether the voice tables exist + their row counts (no error when absent)."""
    declared = handle.descriptor.declared
    emb_table = _capability_table(declared, "voice")
    if not _table_exists(handle, emb_table):
        return VoiceStatusResponse(built=False)
    speakers_table = _capability_table(declared, "speakers")
    speakers = 0
    if _table_exists(handle, speakers_table):
        speakers = int(handle.db.open_table(str(speakers_table)).count_rows())
    return VoiceStatusResponse(
        built=True,
        turns=int(handle.db.open_table(str(emb_table)).count_rows()),
        speakers=speakers,
    )


def _anchor_rows(table: Any, filter_expr: str) -> list[dict[str, Any]]:
    """Filtered scan of a voice table (embedding included) as Python rows."""
    # Pure-metadata scan: ds.to_table(filter=...), never .search().where().
    return table.to_lance().to_table(filter=filter_expr).to_pylist()


def _turn_row_to_anchor(row: dict[str, Any], doc_id: str, embedding_column: str) -> tuple[list[float], VoiceAnchor]:
    anchor = VoiceAnchor(
        doc_id=doc_id,
        speaker_label=row[_TURN_SPEAKER],
        turn_id=int(row[_TURN_ID]),
        turn_start=float(row[_TURN_START]),
        turn_end=float(row[_TURN_END]),
    )
    return row[embedding_column], anchor


def _resolve_turn_anchor(emb_tbl: Any, doc_id: str, turn_id: int, embedding_column: str) -> tuple[list[float], VoiceAnchor]:
    rows = _anchor_rows(emb_tbl, and_(eq(_TURN_DOC, doc_id), eq(_TURN_ID, int(turn_id))))
    if not rows:
        raise NotFoundError("anchor turn not found")
    return _turn_row_to_anchor(rows[0], doc_id, embedding_column)


def _resolve_time_anchor(emb_tbl: Any, doc_id: str, t: float, embedding_column: str) -> tuple[list[float], VoiceAnchor]:
    rows = _anchor_rows(
        emb_tbl,
        and_(eq(_TURN_DOC, doc_id), f"{_TURN_START} <= {float(t)}", f"{_TURN_END} >= {float(t)}"),
    )
    if not rows:
        raise NotFoundError("no speaker turn at that time")
    # Overlapped speech can stack several turns over one instant; the most
    # recently started one is the active speaker — and a deterministic pick.
    return _turn_row_to_anchor(max(rows, key=lambda r: float(r[_TURN_START])), doc_id, embedding_column)


def _resolve_speaker_anchor(speakers_tbl: Any | None, doc_id: str, speaker: str, embedding_column: str) -> tuple[list[float], VoiceAnchor]:
    if speakers_tbl is None:
        raise ServiceUnavailableError("speakers table not built yet — run `ratch build-speakers`")
    rows = _anchor_rows(speakers_tbl, and_(eq(_TURN_DOC, doc_id), eq(_TURN_SPEAKER, speaker)))
    if not rows:
        raise NotFoundError("anchor speaker not found")
    row = rows[0]
    return row[embedding_column], VoiceAnchor(doc_id=doc_id, speaker_label=row[_TURN_SPEAKER])


def _search_turns(
    emb_tbl: Any,
    embedding_column: str,
    vec: list[float],
    *,
    n: int,
    exclude_doc_id: str | None,
) -> list[dict[str, Any]]:
    """Cosine kNN over per-turn voiceprints (explicit ``vector_column_name``)."""
    try:
        qb = (
            emb_tbl.search(vec, vector_column_name=embedding_column)
            .distance_type("cosine")
            .minimum_nprobes(_VECTOR_NPROBES)
            .maximum_nprobes(_VECTOR_MAX_NPROBES)
            .refine_factor(_VECTOR_REFINE_FACTOR)
            .select([*_TURN_HIT_COLUMNS, "_distance"])
            .limit(n)
        )
        if exclude_doc_id is not None:
            qb = qb.where(ne(_TURN_DOC, exclude_doc_id), prefilter=True)
        return qb.to_list()
    except Exception as e:
        logger.warning("voice search failed", exc_info=True)
        raise ValidationError("voice search failed") from e


def _chunk_for_turn(row_ds: Any, bindings: VoiceBindings, doc_id: str, turn_start: float, turn_end: float) -> dict[str, Any] | None:
    """The max-overlap row-table row for a turn span (None if no chunk overlaps)."""
    flt = and_(
        eq(bindings.doc_key, doc_id),
        f"{bindings.time_start} < {turn_end}",
        f"{bindings.time_end} > {turn_start}",
    )
    rows = row_ds.to_table(columns=bindings.payload_columns, filter=flt).to_pylist()
    if not rows:
        return None
    return max(
        rows,
        key=lambda r: min(float(r[bindings.time_end]), turn_end) - max(float(r[bindings.time_start]), turn_start),
    )


def _chunk_key(bindings: VoiceBindings, row: dict[str, Any]) -> tuple[str, ...]:
    """The identity-key tuple shared by the row and frames tables (stringified
    so an int32-vs-int64 read difference can never split a key)."""
    return tuple(str(row[f]) for f in bindings.key_fields)


def _attach_captions(handle: DatasetHandle, bindings: VoiceBindings, hits: list[dict[str, Any]]) -> None:
    """Set ``hit[<caption column>]`` from each chunk's representative frame.

    One batched ``doc IN (...)`` scan of the captions table; ``frame_idx`` is
    matched in Python, not SQL (known Lance planner bug with frame_idx
    predicates). A no-op (leaves no caption key) when the captions capability
    or its column is absent — captions are decorative, never a reason to fail.
    """
    if not hits or bindings.captions_table is None or bindings.captions_column is None:
        return
    if not _table_exists(handle, bindings.captions_table):
        return
    frames_tbl: Any = handle.db.open_table(bindings.captions_table)
    if bindings.captions_column not in frames_tbl.schema.names:
        return
    doc_filter = isin(bindings.doc_key, (str(h[bindings.doc_key]) for h in hits))
    try:
        rows = (
            frames_tbl.to_lance()
            .to_table(
                columns=[*bindings.key_fields, _FRAME_IDX, bindings.captions_column],
                filter=doc_filter,
            )
            .to_pylist()
        )
    except Exception:
        logger.warning("caption attach failed", exc_info=True)
        return
    by_key = {_chunk_key(bindings, r): r.get(bindings.captions_column) for r in rows if int(r[_FRAME_IDX]) == 0}
    for h in hits:
        h[bindings.captions_column] = by_key.get(_chunk_key(bindings, h))


def _attach_speaker_clusters(handle: DatasetHandle, bindings: VoiceBindings, hits: list[dict[str, Any]]) -> None:
    """Set ``hit['speaker_cluster']`` from the speakers table (``None`` = unclustered).

    One batched ``doc IN (...)`` read per request, then a Python-side
    ``(doc, speaker_label)`` pick. The field is uniformly present on every hit:
    ``None`` when the speakers table / cluster column is absent or the speaker
    is clustering noise (-1), so the frontend never branches on key existence.
    """
    for h in hits:
        h["speaker_cluster"] = None
    if not hits or bindings.speakers_table is None:
        return
    speakers_tbl: Any = handle.db.open_table(bindings.speakers_table)
    if _SPEAKER_CLUSTER not in speakers_tbl.schema.names:
        return
    doc_filter = isin(_TURN_DOC, (str(h[bindings.doc_key]) for h in hits))
    try:
        rows = (
            speakers_tbl.to_lance()
            .to_table(
                columns=[_TURN_DOC, _TURN_SPEAKER, _SPEAKER_CLUSTER],
                filter=doc_filter,
            )
            .to_pylist()
        )
    except Exception:
        logger.warning("speaker_cluster attach failed", exc_info=True)
        return
    by_key = {(r[_TURN_DOC], r[_TURN_SPEAKER]): r[_SPEAKER_CLUSTER] for r in rows}
    for h in hits:
        cluster = by_key.get((str(h[bindings.doc_key]), h[_TURN_SPEAKER]))
        if cluster is not None and int(cluster) >= 0:
            h["speaker_cluster"] = int(cluster)


def _postprocess_hits(handle: DatasetHandle, bindings: VoiceBindings, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Finalize hits into the uniform search Hit shape.

    ``alignments`` ships empty by contract: the per-word timing blob is never
    projected into a search/voice payload (it is ~93% of the bytes and only the
    player renders it, via its own lazy endpoint).
    """
    for h in hits:
        h["alignments"] = []
    _attach_captions(handle, bindings, hits)
    return hits


def rank_similar_turns(
    handle: DatasetHandle,
    bindings: VoiceBindings,
    vec: list[float],
    *,
    n: int,
    exclude_doc_id: str | None,
) -> list[dict[str, Any]]:
    """The shared post-anchor path: clamp ``n`` → kNN → turn→chunk join → postprocess.

    Every anchor form (the GET's Lance-resolved voiceprints and the POST's
    uploaded-snippet embedding) funnels through here, so hits keep one shape —
    including ``speaker_cluster``, joined from the speakers table when present.
    """
    n = max(1, min(n, _MAX_N))
    emb_tbl: Any = handle.db.open_table(bindings.embeddings_table)
    turn_hits = _search_turns(emb_tbl, bindings.embedding_column, vec, n=n, exclude_doc_id=exclude_doc_id)

    row_ds: Any = handle.db.open_table(bindings.row_table).to_lance()
    hits: list[dict[str, Any]] = []
    for th in turn_hits:
        turn_start, turn_end = float(th[_TURN_START]), float(th[_TURN_END])
        chunk = _chunk_for_turn(row_ds, bindings, th[_TURN_DOC], turn_start, turn_end)
        if chunk is None:
            continue
        distance = float(th["_distance"])
        chunk["speaker_label"] = th[_TURN_SPEAKER]
        chunk["turn_id"] = int(th[_TURN_ID])
        chunk["turn_start"] = turn_start
        chunk["turn_end"] = turn_end
        chunk["_distance"] = distance
        chunk["turn_score"] = 1.0 - distance
        hits.append(chunk)

    hits = _postprocess_hits(handle, bindings, hits)
    _attach_speaker_clusters(handle, bindings, hits)
    return hits


def similar_voices(
    handle: DatasetHandle,
    *,
    doc_id: str,
    turn_id: int | None,
    speaker: str | None,
    t: float | None,
    n: int,
    exclude_same_doc: bool,
) -> VoiceSimilarResponse:
    """Rank speaker turns by voice similarity to one anchor; return chunk hits.

    Exactly one anchor form is accepted: ``turn_id`` (that turn's embedding),
    ``speaker`` (the speaker's duration-weighted centroid), or ``t`` (the turn
    covering second ``t``). Turns whose span overlaps no chunk are dropped, so
    fewer than ``n`` hits can come back.
    """
    bindings = _require_bindings(handle)
    anchors = [a for a in (turn_id, speaker, t) if a is not None]
    if len(anchors) != 1:
        raise ValidationError("provide exactly one anchor: turn_id, speaker, or t")
    if t is not None and not math.isfinite(t):
        raise ValidationError("t must be a finite number of seconds")

    emb_tbl: Any = handle.db.open_table(bindings.embeddings_table)
    speakers_tbl: Any | None = handle.db.open_table(bindings.speakers_table) if bindings.speakers_table else None
    if turn_id is not None:
        vec, anchor = _resolve_turn_anchor(emb_tbl, doc_id, turn_id, bindings.embedding_column)
    elif speaker is not None:
        vec, anchor = _resolve_speaker_anchor(speakers_tbl, doc_id, speaker, bindings.embedding_column)
    elif t is not None:
        vec, anchor = _resolve_time_anchor(emb_tbl, doc_id, t, bindings.embedding_column)
    else:
        # Unreachable — the exactly-one check above guarantees a branch.
        raise AssertionError("unhandled anchor form")

    hits = rank_similar_turns(
        handle,
        bindings,
        vec,
        n=n,
        exclude_doc_id=doc_id if exclude_same_doc else None,
    )
    return VoiceSimilarResponse(query=anchor, hits=hits)


def speaker_identity(handle: DatasetHandle, *, doc_id: str, speaker: str) -> VoiceIdentityResponse:
    """Resolve one (``doc_id``, ``speaker``) to its global identity cluster.

    ``speaker_cluster`` is ``None`` — with the anchor speaker as its only
    appearance — when the cluster column is absent or the assignment is
    clustering noise (-1). Otherwise the response lists every cluster member,
    most speech first. ``doc_id`` is whitelisted by the router; ``speaker`` is
    quoted here.
    """
    speakers_name = _capability_table(handle.descriptor.declared, "speakers")
    if not _table_exists(handle, speakers_name):
        raise ServiceUnavailableError("speakers table not built yet — run `ratch build-speakers`")
    speakers_tbl: Any = handle.db.open_table(str(speakers_name))
    ds = speakers_tbl.to_lance()
    has_cluster = _SPEAKER_CLUSTER in speakers_tbl.schema.names
    columns = _IDENTITY_COLUMNS + ([_SPEAKER_CLUSTER] if has_cluster else [])
    rows = ds.to_table(
        columns=columns,
        filter=and_(eq(_TURN_DOC, doc_id), eq(_TURN_SPEAKER, speaker)),
    ).to_pylist()
    if not rows:
        raise NotFoundError("speaker not found")
    row = rows[0]
    cluster = row.get(_SPEAKER_CLUSTER)
    if cluster is None or int(cluster) < 0:
        return VoiceIdentityResponse(speaker_cluster=None, appearances=[_appearance(row)], n_videos=1)
    members = ds.to_table(columns=_IDENTITY_COLUMNS, filter=f"{_SPEAKER_CLUSTER} = {int(cluster)}").to_pylist()
    members.sort(key=lambda r: -float(r[_SPEAKER_TOTAL_DURATION]))
    return VoiceIdentityResponse(
        speaker_cluster=int(cluster),
        appearances=[_appearance(r) for r in members],
        n_videos=len({r[_TURN_DOC] for r in members}),
    )


def _appearance(row: dict[str, Any]) -> VoiceIdentityAppearance:
    return VoiceIdentityAppearance(
        doc_id=row[_TURN_DOC],
        speaker_label=row[_TURN_SPEAKER],
        n_turns=row[_SPEAKER_N_TURNS],
        total_duration=row[_SPEAKER_TOTAL_DURATION],
    )


def _decode_upload_wav(file_bytes: bytes) -> np.ndarray:
    """ffmpeg-decode uploaded bytes → 16 kHz mono float32 samples in [-1, 1].

    The bytes go to a temp file (ffmpeg sniffs the container itself — wav/mp3/
    mp4/m4a/…, no trust in the filename), get transcoded to the canonical
    16 kHz mono PCM16 WAV, and are loaded with the encoder's own loader.
    Undecodable input maps to a 400 — it's the uploader's bytes, not our state.
    """
    with tempfile.TemporaryDirectory(prefix="media-voice-upload-") as tmp:
        src = Path(tmp) / "upload.bin"
        src.write_bytes(file_bytes)
        wav = Path(tmp) / "audio_16k_mono.wav"
        try:
            extract_wav_16k_mono(src, wav, timeout=_UPLOAD_FFMPEG_TIMEOUT_S)
        except RuntimeError as e:
            logger.info("upload decode failed: %s", e)
            raise ValidationError("could not decode the upload as audio") from e
        return load_wav_16k_mono(wav)


def similar_voices_for_upload(
    handle: DatasetHandle,
    get_encoder: Callable[[], TurnBatchEncoder],
    *,
    file_bytes: bytes,
    n: int,
) -> VoiceSimilarResponse:
    """Rank speaker turns by voice similarity to an uploaded audio/video snippet.

    The snippet is decoded via ffmpeg, capped to its first
    :data:`_UPLOAD_EMBED_CAP_S` seconds, embedded whole (one "turn") with the
    lazily-loaded CPU encoder, L2-normalized, and ranked through the same
    :func:`rank_similar_turns` path as the GET. The query has no Lance-side
    anchor, so the response's ``query`` is an all-``None`` :class:`VoiceAnchor`;
    ``exclude_same_doc`` does not apply (an upload belongs to no doc). The
    encoder is fetched through ``get_encoder`` only after the size/decode/
    duration guards pass, so a bad upload never loads the model.
    """
    bindings = _require_bindings(handle)
    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        raise ValidationError("upload too large (max 25 MB)")

    wav = _decode_upload_wav(file_bytes)
    if wav.shape[0] < round(MIN_TURN_DURATION_S * TARGET_SAMPLE_RATE):
        raise ValidationError(f"snippet too short for a voiceprint (min {MIN_TURN_DURATION_S} s)")
    wav = wav[: round(_UPLOAD_EMBED_CAP_S * TARGET_SAMPLE_RATE)]

    raw = get_encoder().embed_batch([wav])
    unit = l2_normalize(raw)[0]
    # Degenerate audio (e.g. pure silence) can yield a zero or non-finite raw
    # embedding — l2_normalize's norm floor keeps a zero row zero rather than
    # NaN, hence the norm check. Either would make the cosine kNN undefined,
    # so reject it as the uploader's-input problem it is.
    if not np.all(np.isfinite(unit)) or float(np.linalg.norm(unit)) < 0.5:
        raise ValidationError("could not extract a voiceprint from the upload")
    vec: list[float] = unit.tolist()

    hits = rank_similar_turns(handle, bindings, vec, n=n, exclude_doc_id=None)
    return VoiceSimilarResponse(query=VoiceAnchor(), hits=hits)
