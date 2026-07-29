"""Pure, stateless image/vector transforms for the model clients: L2
normalization and image → data-URL encoding. No network, no model state.

Imported lazily (only when a client actually runs), so its numpy/Pillow deps
stay behind the ``[multimodal]`` extra and never load during `ratch --help`.
"""

from __future__ import annotations

import base64
import io

import numpy as np
from numpy.typing import ArrayLike
from PIL import Image

from ..model.schema import EMBED_DIM


# vLLM sizes the Qwen3-VL deepstack buffer once at warmup; if a runtime image
# yields more vision tokens than that buffer, the engine aborts
# (`num_tokens=N > buffer=N-k`). The robust fix is to send every image at exactly
# the pixel area the server pins via `min_pixels == max_pixels` (see the
# `embed-server` / `embed-server-docker` Makefile targets): 392 x 392 = 153664 px
# == that pin, so the runtime token count can't exceed the warmup ceiling.
# (The previous 448 x 448 = 200704 px overran it — the recurring crash.)
# Center-crop sacrifices aspect ratio — fine for whole-image similarity.
#
# Documented fix per docs/INVESTIGATION.md (Part B); verified end-to-end — the
# full frame-embedding backfill (145k frames) completed under this pin. If you
# change this side length, change the Makefile min/max_pixels pin to match
# (side² == pin) and re-run the INVESTIGATION.md validation gate.
_IMAGE_SIDE = 392

# Re-encode quality for the embedding payload. Trades wire size against fidelity
# only — JPEG quality does NOT affect the vision-token count (that is pinned by
# pixel area via _IMAGE_SIDE above). Whole-image similarity tolerates mild
# compression artifacts, so a standard high-quality setting suffices.
_EMBED_JPEG_QUALITY = 88


def l2_normalize(vectors: ArrayLike) -> np.ndarray:
    """L2-normalize a batch of embeddings to unit length.

    Validates the dimension against ``EMBED_DIM`` so a server/model mismatch
    fails loudly here instead of corrupting the Lance vector column.
    """
    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    if arr.shape[1] != EMBED_DIM:
        raise ValueError(f"expected {EMBED_DIM}-d embeddings, got {arr.shape[1]}-d")
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return (arr / norms).astype(np.float32)


def image_to_data_url(image: Image.Image | bytes | bytearray) -> str:
    """Encode a PIL image or raw JPEG bytes as a ``data:image/jpeg;base64,…`` URL.

    Center-crops + resizes to a fixed square first (see ``_IMAGE_SIDE``) so the
    vLLM vision-token count matches the server's warmup profile.

    This square-crop is a constraint of the *embedding* server only. The
    generative caption client uses :func:`frame_to_data_url` instead, which
    preserves the whole frame (a VLM caption wants the full scene, and a
    chat-completions server sizes vision tokens per request rather than from a
    fixed warmup buffer).
    """
    if isinstance(image, bytes | bytearray):
        image = Image.open(io.BytesIO(bytes(image)))
    if not isinstance(image, Image.Image):
        raise TypeError(f"expected PIL.Image or bytes, got {type(image).__name__}")
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image = _square_crop(image)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=_EMBED_JPEG_QUALITY)
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


# Longest-side cap for caption frames. Extracted frames are ~448 px wide
# (media.frames.DEFAULT_FRAME_WIDTH), so this only downscales the rare larger
# frame and never upscales — Gemma's variable-resolution vision tower handles
# the native aspect ratio fine, and a chat server bills vision tokens per call.
_CAPTION_MAX_SIDE = 896

# Re-encode quality for caption frames — slightly above the embedding payload so
# on-screen text / fine detail stays legible for the caption VLM. Affects payload
# size only, never the vision-token count.
_CAPTION_JPEG_QUALITY = 90


def frame_to_data_url(image: Image.Image | bytes | bytearray, *, max_side: int = _CAPTION_MAX_SIDE) -> str:
    """Encode a frame for a generative VLM caption — full scene, no square crop.

    Preserves aspect ratio, only downscaling so the longest side ≤ ``max_side``.
    Unlike :func:`image_to_data_url` (which pins a square for the embedding
    server's fixed warmup buffer), this keeps the whole frame so the caption
    model sees everything in the shot.
    """
    if isinstance(image, bytes | bytearray):
        image = Image.open(io.BytesIO(bytes(image)))
    if not isinstance(image, Image.Image):
        raise TypeError(f"expected PIL.Image or bytes, got {type(image).__name__}")
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    longest = max(image.size)
    if longest > max_side:
        scale = max_side / longest
        new_size = (round(image.width * scale), round(image.height * scale))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=_CAPTION_JPEG_QUALITY)
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


def _square_crop(image: Image.Image) -> Image.Image:
    w, h = image.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    image = image.crop((left, top, left + side, top + side))
    if image.size != (_IMAGE_SIDE, _IMAGE_SIDE):
        image = image.resize((_IMAGE_SIDE, _IMAGE_SIDE), Image.Resampling.LANCZOS)
    return image
