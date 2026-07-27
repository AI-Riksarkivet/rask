"""Pure, stateless image/vector transforms for the query encoders: L2
normalization and image → data-URL encoding. No network, no model state.

Vendored from ``packages/ratch/clients/image.py`` (backend split, §4.4: no
``ratch`` imports in ``backend/``), trimmed to the query-time surface — the
caption-frame encoding stays in the pipeline package. The pipeline's embedding
dimension constant is replaced by an ``expected_dim`` parameter supplied by the
caller (from the dataset descriptor's vector binding).
"""

from __future__ import annotations

import base64
import io

import numpy as np
from numpy.typing import ArrayLike
from PIL import Image


# vLLM sizes the Qwen3-VL deepstack buffer once at warmup; if a runtime image
# yields more vision tokens than that buffer, the engine aborts
# (`num_tokens=N > buffer=N-k`). The robust fix is to send every image at exactly
# the pixel area the server pins via `min_pixels == max_pixels` (see the
# `embed-server` / `embed-server-docker` Makefile targets): 392 x 392 = 153664 px
# == that pin, so the runtime token count can't exceed the warmup ceiling.
# (The previous 448 x 448 = 200704 px overran it — the recurring crash.)
# Center-crop sacrifices aspect ratio — fine for whole-image similarity.
#
# Documented fix per docs/INVESTIGATION.md (Part B). If you change this side
# length, change the Makefile min/max_pixels pin to match (side² == pin).
_IMAGE_SIDE = 392

# Re-encode quality for the embedding payload. Trades wire size against fidelity
# only — JPEG quality does NOT affect the vision-token count (that is pinned by
# pixel area via _IMAGE_SIDE above). Whole-image similarity tolerates mild
# compression artifacts, so a standard high-quality setting suffices.
_EMBED_JPEG_QUALITY = 88


def l2_normalize(vectors: ArrayLike, *, expected_dim: int) -> np.ndarray:
    """L2-normalize a batch of embeddings to unit length.

    Validates the dimension against ``expected_dim`` (the descriptor binding's
    declared dim) so a server/model mismatch fails loudly here instead of
    corrupting a cosine search with mis-sized query vectors.
    """
    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    if arr.shape[1] != expected_dim:
        raise ValueError(f"expected {expected_dim}-d embeddings, got {arr.shape[1]}-d")
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return (arr / norms).astype(np.float32)


def image_to_data_url(image: Image.Image | bytes | bytearray) -> str:
    """Encode a PIL image or raw JPEG bytes as a ``data:image/jpeg;base64,…`` URL.

    Center-crops + resizes to a fixed square first (see ``_IMAGE_SIDE``) so the
    vLLM vision-token count matches the embedding server's warmup profile.
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


def _square_crop(image: Image.Image) -> Image.Image:
    w, h = image.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    image = image.crop((left, top, left + side, top + side))
    if image.size != (_IMAGE_SIDE, _IMAGE_SIDE):
        image = image.resize((_IMAGE_SIDE, _IMAGE_SIDE), Image.Resampling.LANCZOS)
    return image
