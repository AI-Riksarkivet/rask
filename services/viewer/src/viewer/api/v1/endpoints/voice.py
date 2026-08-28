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

from fastapi import APIRouter, File, Query, UploadFile
from starlette.concurrency import run_in_threadpool

from service_kit.exceptions import ServiceUnavailableError, ValidationError
from service_kit.media.deps import StateDep
from service_kit.media.state import AppState, dataset_handle
from viewer.api.security import REQUIRE_CORPUS_DATA, REQUIRE_CORPUS_METADATA
from viewer.schemas.voice import VoiceIdentityResponse, VoiceSimilarResponse, VoiceStatusResponse
from viewer.services import voice_service
from viewer.services.voice_service import MAX_N


if TYPE_CHECKING:
    from service_kit.lancekit.registry import DatasetHandle
    from viewer.services.serve_encoder import ServeEncoder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice", tags=["voice"])


def require_valid_doc_id(handle: "DatasetHandle", doc_id: str) -> None:
    """Whitelist ``doc_id`` against the descriptor's identity pattern.

    Runs BEFORE the value is inlined into any Lance filter literal — otherwise
    a crafted doc id is a SQL-injection vector.
    """
    if re.fullmatch(handle.descriptor.declared.identity.doc_key_pattern, doc_id) is None:
        raise ValidationError("invalid doc_id")


def voice_encoder(state: AppState) -> "ServeEncoder":
    """The encoder for the upload form — the voiceprint runner's Serve app, over HTTP.

    This used to load pyannote's WeSpeaker IN-PROCESS (~30 s of import + weights), which put a named
    model for one modality inside the shared read plane and duplicated the encoder
    `runners/voiceprint` already seals. It also had to be pinned to CPU so an upload could not
    contend with the vLLM servers for the GPUs — a constraint that only existed because it ran in the
    wrong process.

    An unset endpoint is a 503 NAMING the missing runner rather than a generic failure: the read
    plane is working and the model is simply not deployed, and those lead an operator to different
    places. The Lance-anchored GET forms run no encoder and are unaffected either way.
    """
    from viewer.core.config import get_viewer_settings
    from viewer.services.serve_encoder import ServeEncoder

    url = get_viewer_settings().voiceprint_serve_url
    if not url:
        raise ServiceUnavailableError(
            "the voiceprint runner is not configured (VIEWER_VOICEPRINT_SERVE_URL) — deploy its Ray "
            "Serve app to enable snippet upload; the Lance-anchored /similar and /identity forms need no encoder"
        )
    return ServeEncoder(url)


@router.get("/status", dependencies=[REQUIRE_CORPUS_METADATA])
def voice_status(state: StateDep, dataset: str | None = None) -> VoiceStatusResponse:
    """Whether the voice tables exist + their row counts (no error when absent)."""
    return voice_service.voice_status(dataset_handle(state, dataset))


@router.get("/similar", dependencies=[REQUIRE_CORPUS_DATA])
def voice_similar(
    state: StateDep,
    doc_id: str,
    turn_id: int | None = None,
    speaker: str | None = None,
    t: float | None = None,
    # DECLARED, not clamped: `voice_service` applies `max(1, min(n, _MAX_N))`, so the schema
    # advertised an unbounded integer for a vector-search fan-out.
    n: Annotated[int, Query(ge=1, le=MAX_N)] = 20,
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


@router.get("/identity", dependencies=[REQUIRE_CORPUS_DATA])
def voice_identity(state: StateDep, doc_id: str, speaker: str, dataset: str | None = None) -> VoiceIdentityResponse:
    """The global identity cluster for one (``doc_id``, ``speaker``).

    503 until the speakers table has been built; 404 for an unknown speaker;
    ``speaker_cluster`` is ``None`` (with the anchor as its only appearance)
    until a clustering pass has assigned it a cluster.
    """
    handle = dataset_handle(state, dataset)
    require_valid_doc_id(handle, doc_id)
    return voice_service.speaker_identity(handle, doc_id=doc_id, speaker=speaker)


@router.post("/similar", dependencies=[REQUIRE_CORPUS_DATA])
async def voice_similar_upload(
    state: StateDep,
    file: Annotated[UploadFile, File()],
    # DECLARED, not clamped: `voice_service` applies `max(1, min(n, _MAX_N))`, so the schema
    # advertised an unbounded integer for a vector-search fan-out.
    n: Annotated[int, Query(ge=1, le=MAX_N)] = 20,
    dataset: str | None = None,
) -> VoiceSimilarResponse:
    """Voice-ranked hits for an uploaded snippet (any container ffmpeg decodes).

    ``n`` rides as a query param like the GET's (the multipart body carries
    only ``file``); ``exclude_same_doc`` is deliberately not a param — an
    upload belongs to no doc.

    The body read stops one byte past the size cap, so an oversize upload 400s
    in the service with a message naming the limit. That bounds THIS HANDLER'S
    MEMORY and nothing else. This docstring used to claim the read-cap also kept
    the upload from being buffered in its entirety, and that was false: starlette
    spools a multipart file part to a SpooledTemporaryFile in FULL before the
    handler is ever entered, so by the time this cap runs the bytes have already
    landed. The landing zone is bounded at the door instead, by
    ``BodySizeLimitMiddleware`` (pure ASGI, refuses as bytes arrive); the read cap
    stays as defence in depth.

    The encoder getter is passed as a thunk so the model only loads if the
    snippet survives the size/decode/duration guards.
    """
    # dataset_handle opens Lance on a cold miss — offload it so the async upload
    # handler never blocks the event loop (fastapi skill: no blocking I/O in async def).
    handle = await run_in_threadpool(dataset_handle, state, dataset)
    file_bytes = await file.read(voice_service._MAX_UPLOAD_BYTES + 1)
    return await run_in_threadpool(
        voice_service.similar_voices_for_upload,
        handle,
        lambda: voice_encoder(state),
        file_bytes=file_bytes,
        n=n,
    )
