"""Voice endpoints — table status + "find this voice elsewhere" similarity.

Thin HTTP layer over :mod:`viewer.services.voice_service`: it resolves the
request's dataset (optional ``dataset`` query param, ``None`` → the default
DB), whitelists ``doc_id`` against the descriptor's identity pattern before
any service code inlines it into a Lance filter literal, and owns the
lock-guarded lazy voice encoder on ``state.voice_encoder``. The GET handlers
stay sync — every read is a blocking Lance call, which the threadpool absorbs;
the upload POST is async to await the multipart body, then offloads the
blocking decode + CPU embed + Lance reads to the threadpool.

No ``from __future__ import annotations`` here: FastAPI introspects these
signatures at runtime, so the annotations stay real objects.
"""

import logging
import re
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, File, UploadFile
from starlette.concurrency import run_in_threadpool

from service_kit.media.deps import StateDep
from service_kit.media.exceptions import ServiceUnavailableError, ValidationError
from service_kit.media.state import AppState, dataset_handle
from service_kit.schemas.voice import VoiceIdentityResponse, VoiceSimilarResponse, VoiceStatusResponse
from viewer.services import voice_service


if TYPE_CHECKING:
    from service_kit.lancekit.registry import DatasetHandle
    from viewer.services.wespeaker import VoiceEncoder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice", tags=["voice"])


def require_valid_doc_id(handle: "DatasetHandle", doc_id: str) -> None:
    """Whitelist ``doc_id`` against the descriptor's identity pattern.

    Runs BEFORE the value is inlined into any Lance filter literal — otherwise
    a crafted doc id is a SQL-injection vector.
    """
    if re.fullmatch(handle.descriptor.declared.identity.doc_key_pattern, doc_id) is None:
        raise ValidationError("invalid doc_id")


def ensure_voice_encoder(state: AppState) -> "VoiceEncoder":
    """Return the app's voice encoder, loading it on first use (503 on failure).

    Loads a model **in-process** (pyannote's WeSpeaker encoder, ~30 s of import
    + weights), so first-load is guarded by ``state.voice_encoder_lock`` — sync
    handlers run in a threadpool, and two concurrent first uploads must not
    both construct it. CPU by design: the upload path must never contend with
    the vLLM servers for the GPUs.
    """
    if state.voice_encoder is not None:
        return state.voice_encoder
    with state.voice_encoder_lock:
        if state.voice_encoder is not None:  # lost the race; the winner loaded it
            return state.voice_encoder
        try:
            from viewer.services.wespeaker import VoiceEncoder

            encoder = VoiceEncoder(device="cpu")
        except Exception as e:
            logger.exception("failed to load voice encoder")
            raise ServiceUnavailableError("voice encoder unavailable") from e
        state.voice_encoder = encoder
        return encoder


@router.get("/status")
def voice_status(state: StateDep, dataset: str | None = None) -> VoiceStatusResponse:
    """Whether the voice tables exist + their row counts (no error when absent)."""
    return voice_service.voice_status(dataset_handle(state, dataset))


@router.get("/similar")
def voice_similar(
    state: StateDep,
    doc_id: str,
    turn_id: int | None = None,
    speaker: str | None = None,
    t: float | None = None,
    n: int = 20,
    exclude_same_doc: bool = True,
    dataset: str | None = None,
) -> VoiceSimilarResponse:
    """Voice-ranked hits for exactly one anchor: ``turn_id`` | ``speaker`` | ``t``.

    The anchor embedding is read from Lance (no encoder at query time); ``n``
    is clamped to the service's cap. ``rerank`` is deliberately not offered —
    the cross-encoder scores transcript text, which says nothing about voice.
    """
    handle = dataset_handle(state, dataset)
    require_valid_doc_id(handle, doc_id)
    return voice_service.similar_voices(
        handle,
        doc_id=doc_id,
        turn_id=turn_id,
        speaker=speaker,
        t=t,
        n=n,
        exclude_same_doc=exclude_same_doc,
    )


@router.get("/identity")
def voice_identity(state: StateDep, doc_id: str, speaker: str, dataset: str | None = None) -> VoiceIdentityResponse:
    """The global identity cluster for one (``doc_id``, ``speaker``).

    503 until the speakers table has been built; 404 for an unknown speaker;
    ``speaker_cluster`` is ``None`` (with the anchor as its only appearance)
    until a clustering pass has assigned it a cluster.
    """
    handle = dataset_handle(state, dataset)
    require_valid_doc_id(handle, doc_id)
    return voice_service.speaker_identity(handle, doc_id=doc_id, speaker=speaker)


@router.post("/similar")
async def voice_similar_upload(
    state: StateDep,
    file: Annotated[UploadFile, File()],
    n: int = 20,
    dataset: str | None = None,
) -> VoiceSimilarResponse:
    """Voice-ranked hits for an uploaded snippet (any container ffmpeg decodes).

    ``n`` rides as a query param like the GET's (the multipart body carries
    only ``file``); ``exclude_same_doc`` is deliberately not a param — an
    upload belongs to no doc. The body read stops one byte past the size cap,
    so an oversize upload 400s (in the service) without ever being buffered
    whole. The encoder getter is passed as a thunk so the model only loads if
    the snippet survives the size/decode/duration guards.
    """
    # dataset_handle opens Lance on a cold miss — offload it so the async upload
    # handler never blocks the event loop (fastapi skill: no blocking I/O in async def).
    handle = await run_in_threadpool(dataset_handle, state, dataset)
    file_bytes = await file.read(voice_service._MAX_UPLOAD_BYTES + 1)
    return await run_in_threadpool(
        voice_service.similar_voices_for_upload,
        handle,
        lambda: ensure_voice_encoder(state),
        file_bytes=file_bytes,
        n=n,
    )
