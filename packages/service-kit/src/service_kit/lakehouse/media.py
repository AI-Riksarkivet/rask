"""Media derivation — decode a blob-v2 image into lightweight, inline artifacts.

The heavy original image stays a referenced blob in bronze; a downstream (silver) stage derives small
artifacts from it that are cheap to store *inline*: a thumbnail (downscaled PNG) and a fixed-size embedding
(deterministic pixel features — no ML model). This is the realistic per-stage transform the generic cascade
mover leaves to a distributed job (see ``compute.py``); here it runs in-process so the medallion demo shows
a real multimodal pipeline end to end.

**WHY THIS IS IN service-kit AND NOT THE MEDALLION SERVICE (B14).** The same three derivers were
INLINED into ``scripts/ray_stage_job.py``, because that script is baked into the Ray cluster image
and cannot import a service. A drift-pin test then asserted the two copies stayed byte-identical —
which is a test comparing two behaviours after the fact, and is exactly what B14 records as the
wrong fix. Both drivers can import ``service_kit``, so the implementation moved here and both now
call ONE function. The script stays a thin entrypoint, which is the shape B14 asks for.

Pillow rides an OPTIONAL extra (``service-kit[media]``) rather than the base dependency set: this
library is deliberately dependency-light, and an image codec has no business in the install of a
consumer that only wants the settings helpers.
"""

from __future__ import annotations

import io
import warnings

from PIL import Image


THUMBNAIL_SIZE = (128, 128)
EMBEDDING_DIMS = 8

# Decompression-bomb ceiling (audit 2026-07-12): Pillow's DEFAULT raises only above 2× its limit —
# the [limit, 2×limit) band merely WARNS and then fully decodes, so a small-on-disk ~150M-pixel PNG
# would pass is_image() and allocate ~half a GB of pixel buffer in the deriver. Cap at a bound that
# comfortably covers real photos (64MP ≈ any camera); _open_guarded turns the WARNING band into a
# hard raise PER OPEN (a scoped filter, not a global one — pytest and other frameworks reset global
# warning filters, which would silently disarm the guard), so a bomb is rejected as not-an-image /
# underivable (DROP) instead of decoded.
Image.MAX_IMAGE_PIXELS = 64_000_000


def _open_guarded(payload: bytes) -> Image.Image:
    """Open image bytes with the decompression-bomb WARNING band promoted to an error."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        return Image.open(io.BytesIO(payload))


def is_image(payload: bytes) -> bool:
    """Whether ``payload`` decodes as an image — the content probe for artifact derivation.

    A blob column is not necessarily image media (audio/docs are blobs too); the cascade probes the first
    payload and only derives thumbnails/embeddings for decodable image datasets, carrying anything else
    forward untouched.
    """
    try:
        with _open_guarded(payload) as image:
            image.verify()
    except Exception:
        return False
    return True


def derive_thumbnail(image_bytes: bytes, size: tuple[int, int] = THUMBNAIL_SIZE) -> bytes:
    """A downscaled PNG thumbnail of ``image_bytes`` — small enough to store INLINE (not a blob)."""
    with _open_guarded(image_bytes) as image:
        thumbnail = image.convert("RGB")
        thumbnail.thumbnail(size)
        buffer = io.BytesIO()
        thumbnail.save(buffer, format="PNG")
        return buffer.getvalue()


def derive_embedding(image_bytes: bytes, dims: int = EMBEDDING_DIMS) -> list[float]:
    """A deterministic ``dims``-length feature vector — the luminance of the image downsampled to ``dims``
    columns. Real pixel-derived features (not a random vector), so silver's embedding actually depends on
    the media, without pulling in an ML-model dependency.
    """
    with _open_guarded(image_bytes) as image:
        columns = image.convert("L").resize((dims, 1), resample=Image.Resampling.BILINEAR)
        # An 8-bit "L" image's tobytes() is one byte per pixel, so iterating yields ints (0-255); the
        # explicit resample pins the downsample so the embedding is deterministic across environments.
        return [value / 255.0 for value in columns.tobytes()]
