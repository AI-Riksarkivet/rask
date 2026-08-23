"""Ray Serve door for the speaker encoder — the warm-weights half of `runners/voiceprint`.

The estate's rule is that long-lived model weights stay warm behind a Ray Serve deployment the
runner owns, deployed independently of any job. This is voiceprint's.

WHY IT EXISTS AT ALL. `services/viewer` used to load pyannote's WeSpeaker encoder IN-PROCESS to
serve one endpoint — an upload of an audio snippet to rank against known voiceprints — which put a
named model for one modality inside the shared read plane every modality's reads go through, and
made a second copy of the encoder this runner already seals. The viewer now POSTs the snippet here.

CPU IS NOT THE DEFAULT ANY MORE, and that is part of the point. The in-process encoder was pinned to
CPU deliberately, so an upload could not contend with the vLLM servers for the GPUs — a constraint
that only existed because it ran in the wrong process. Here it is the runner's own business: the
actor options decide, like every other runner's.

THE BODY IS A DECODED WAVEFORM, not the uploaded container, and that split is deliberate. ffmpeg
transcoding is model-free — `packages/ratch/modalities/av/wav.py` calls it "an external transcoder, no
inference" — so it stays with the caller, where the size, duration and finiteness guards on an
uploader's input already run. Sending the container instead would move those guards behind a network
hop and let a malformed upload reach the model before anything checked it.

So: raw little-endian float32 samples at 16 kHz mono, bare; the response is the embedding as JSON,
which is small. That is exactly the `embed_batch([wav])` contract the caller already had, with a
network in the middle.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from ray import serve


if TYPE_CHECKING:
    import numpy as np
    from starlette.requests import Request


logger = logging.getLogger(__name__)

#: Replicas and GPU share are the RUNNER's business, never the platform's — same env names every
#: other runner's Serve deployment reads.
SERVE_REPLICAS: int = int(os.getenv("RASK_SERVE_REPLICAS", "1") or 1)
SERVE_GPU_FRAC: float = float(os.getenv("RASK_SERVE_GPU_FRAC", "0") or 0)

#: Sample rate the speaker models expect. Declared for the refusal below, not to resample: a caller
#: sending anything else has a decode bug, and silently accepting it would embed nonsense.
TARGET_SAMPLE_RATE: int = 16_000

#: Refuse a body that is not a whole number of float32 samples, or is implausibly long. A ceiling
#: rather than a truncation: the encoder was trained on short chunks, so a longer snippet is a caller
#: mistake worth naming rather than something to silently trim.
MAX_SAMPLES: int = int(float(os.getenv("RASK_VOICEPRINT_MAX_SECONDS", "30") or 30) * TARGET_SAMPLE_RATE)

_ACTOR_OPTIONS: dict[str, Any] = {"num_gpus": SERVE_GPU_FRAC} if SERVE_GPU_FRAC > 0 else {}


@serve.deployment(
    name="VoiceprintService",
    num_replicas=SERVE_REPLICAS,
    ray_actor_options=_ACTOR_OPTIONS,
    max_ongoing_requests=4,
)
class VoiceprintDeployment:
    """Holds the speaker encoder warm and answers one question: bytes in, embedding out."""

    def __init__(self) -> None:
        # Deferred so the deployment class stays cheaply picklable — torch modules misbehave when
        # imported at module scope and then pickled by Ray, which is the same reason the HTR
        # deployment defers its imports.
        from runners.voiceprint.voiceprint import VoiceEncoder

        device = "cuda" if SERVE_GPU_FRAC > 0 else "cpu"
        logger.info("VoiceprintDeployment: loading speaker encoder on %s", device)
        self._encoder = VoiceEncoder(device=device)
        logger.info("VoiceprintDeployment: ready")

    def embed(self, waveform: np.ndarray) -> list[float]:
        """One 16 kHz mono waveform -> its voiceprint."""
        return [float(v) for v in self._encoder.embed_batch([waveform])[0]]

    async def __call__(self, request: Request) -> Any:
        """HTTP ingress: POST little-endian float32 samples -> `{"embedding": [...]}`.

        Every refusal is a 400 naming what was wrong, because each one is a caller bug with an
        actionable fix — an empty body, a truncated frame, an implausible length. A 500 from the model
        would report the encoder as broken for a request that never should have reached it.
        """
        import numpy as np
        from starlette.responses import JSONResponse

        body = await request.body()
        if not body:
            return JSONResponse({"detail": "empty body — POST float32 samples at 16 kHz mono"}, status_code=400)
        if len(body) % 4:
            return JSONResponse({"detail": f"{len(body)} bytes is not a whole number of float32 samples"}, status_code=400)
        samples = len(body) // 4
        if samples > MAX_SAMPLES:
            return JSONResponse(
                {"detail": f"{samples} samples exceeds the {MAX_SAMPLES}-sample ceiling (RASK_VOICEPRINT_MAX_SECONDS)"},
                status_code=400,
            )
        waveform = np.frombuffer(body, dtype="<f4")
        return JSONResponse({"embedding": self.embed(waveform)})


voiceprint_app = VoiceprintDeployment.bind()
