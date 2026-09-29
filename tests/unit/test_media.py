"""§9 P3 media derivation — thumbnail / embedding / caption from a real image."""

from __future__ import annotations

import io

import pytest
from PIL import Image, UnidentifiedImageError

from service_kit.lakehouse import media


def _png(color: tuple[int, int, int] = (10, 20, 30), size: tuple[int, int] = (64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_decompression_bomb_warning_band_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit 2026-07-12: Pillow's default only RAISES above 2x MAX_IMAGE_PIXELS — the (MAX, 2*MAX)
    band merely warns and then fully decodes (~0.5 GB for a crafted ~150M-pixel PNG). media.py caps
    MAX_IMAGE_PIXELS and promotes the warning to an error, so the band is REJECTED. Pinned with a
    tiny cap so the test needs no giant allocation: 40x40 (1600 px) sits in (1000, 2000) -> bomb."""
    assert Image.MAX_IMAGE_PIXELS == 64_000_000  # the module cap is set on import
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    assert media.is_image(_png(size=(20, 20))) is True  # under the cap: normal image
    assert media.is_image(_png(size=(40, 40))) is False  # warning band: rejected, not decoded


def test_derive_thumbnail_is_a_smaller_png() -> None:
    source = _png(size=(512, 512))
    thumb = media.derive_thumbnail(source, size=(32, 32))
    with Image.open(io.BytesIO(thumb)) as image:
        assert image.format == "PNG"
        assert max(image.size) <= 32
    assert len(thumb) < len(source)


def test_derive_embedding_is_fixed_size_and_deterministic() -> None:
    source = _png()
    assert media.derive_embedding(source) == media.derive_embedding(source)
    embedding = media.derive_embedding(source)
    assert len(embedding) == media.EMBEDDING_DIMS
    assert all(0.0 <= value <= 1.0 for value in embedding)


def test_derive_from_non_image_raises_cleanly() -> None:
    with pytest.raises(UnidentifiedImageError):
        media.derive_thumbnail(b"not-an-image")
    with pytest.raises(UnidentifiedImageError):
        media.derive_embedding(b"not-an-image")
