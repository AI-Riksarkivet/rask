"""An embedding encoder over HTTP — a `TurnBatchEncoder` backed by a runner's Ray Serve app.

This replaces an in-process pyannote WeSpeaker encoder that used to load inside the read plane. That
put a named model for one modality in the service every modality's reads go through, and made a
second copy of an encoder `runners/voiceprint` already seals. Long-lived model weights in this estate
stay warm behind a Ray Serve deployment the runner owns; this is the caller side of that.

IT SENDS A DECODED WAVEFORM, not the uploaded container. ffmpeg transcoding is model-free, so it
stays in the viewer where the size, duration and finiteness guards on an uploader's input already
run — sending the container instead would move those guards behind a network hop and let a malformed
upload reach the model before anything checked it.

The shape it satisfies is unchanged: `embed_batch(rows) -> ndarray`. The caller was already written
against a thunk returning that protocol, so nothing in the retrieval path knows the encoder moved.

NOTHING HERE IS MODALITY-SPECIFIC, and the name says so: it posts float32 rows to a configured URL
and reads embeddings back. Which runner answers is a setting, so the same client serves any embedding
workload — a module named after one would have made the seam read as that workload's.
"""

from __future__ import annotations

import logging

import httpx
import numpy as np

from service_kit.exceptions import ServiceUnavailableError


logger = logging.getLogger(__name__)


class ServeEncoder:
    """Posts rows to a runner's Serve endpoint, one request per row.

    ONE PER REQUEST, deliberately. The batch form exists for the pipeline, which embeds thousands of
    turns; this path embeds a single uploaded snippet, so batching would add an envelope for a list
    of one. If a caller ever needs true batching the door takes it — this client is the one that does
    not need it.
    """

    def __init__(self, base_url: str, *, timeout_seconds: float = 30.0) -> None:
        self._url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def embed_batch(self, waveforms: list[np.ndarray]) -> np.ndarray:
        """Embed each waveform and return the rows stacked, exactly as the in-process encoder did.

        A transport failure is a 503 naming the runner rather than an opaque 500: the read plane is
        working and the model is not reachable, and those lead an operator to different places.
        """
        rows: list[np.ndarray] = []
        try:
            with httpx.Client(timeout=self._timeout) as client:
                for waveform in waveforms:
                    body = np.ascontiguousarray(waveform, dtype="<f4").tobytes()
                    response = client.post(self._url, content=body, headers={"content-type": "application/octet-stream"})
                    if response.status_code >= 400:
                        raise ServiceUnavailableError(f"voiceprint runner refused the embed ({response.status_code}): {response.text[:200]}")
                    rows.append(np.asarray(response.json()["embedding"], dtype=np.float32))
        except httpx.HTTPError as exc:
            logger.warning("voiceprint runner unreachable at %s", self._url, exc_info=True)
            raise ServiceUnavailableError(f"voiceprint runner unreachable at {self._url}") from exc
        return np.vstack(rows)
