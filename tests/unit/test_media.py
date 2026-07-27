"""§9 P3 media derivation — thumbnail / embedding / caption from a real image."""

from __future__ import annotations

import io
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from common import blobs
from lance import blob_array, blob_field
from medallion.services import media
from PIL import Image, UnidentifiedImageError


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


def test_embedding_depends_on_the_image() -> None:
    assert media.derive_embedding(_png((0, 0, 0))) != media.derive_embedding(_png((255, 255, 255)))


def test_derive_from_non_image_raises_cleanly() -> None:
    with pytest.raises(UnidentifiedImageError):
        media.derive_thumbnail(b"not-an-image")
    with pytest.raises(UnidentifiedImageError):
        media.derive_embedding(b"not-an-image")


def test_embedding_fits_a_fixed_size_list() -> None:
    # the demo stores the embedding as FixedSizeList(float32, EMBEDDING_DIMS)
    embedding = media.derive_embedding(_png())
    array = pa.array([embedding], type=pa.list_(pa.float32(), media.EMBEDDING_DIMS))
    assert array.type.list_size == media.EMBEDDING_DIMS


def test_media_pipeline_bronze_blob_to_silver_thumbnail_embedding(tmp_path: Path) -> None:
    # the §9 DoD shape end to end: a bronze image blob (2.2) → silver thumbnail + embedding (2.2)
    bronze, silver = str(tmp_path / "bronze"), str(tmp_path / "silver")
    images = [_png((200, 0, 0)), _png((0, 0, 200))]
    lance.write_dataset(
        pa.table(
            {"id": [0, 1], "payload": blob_array(images)},
            schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload")]),
        ),
        bronze,
        data_storage_version="2.2",
    )
    payloads = [p for _addr, p in lance.dataset(bronze).read_blobs("payload", indices=[0, 1])]
    lance.write_dataset(
        pa.table(
            {
                "id": [0, 1],
                "thumbnail": pa.array([media.derive_thumbnail(p) for p in payloads], pa.large_binary()),
                "embedding": pa.array(
                    [media.derive_embedding(p) for p in payloads],
                    type=pa.list_(pa.float32(), media.EMBEDDING_DIMS),
                ),
            },
            schema=pa.schema(
                [
                    pa.field("id", pa.int64()),
                    pa.field("thumbnail", pa.large_binary()),
                    pa.field("embedding", pa.list_(pa.float32(), media.EMBEDDING_DIMS)),
                ]
            ),
        ),
        silver,
        data_storage_version="2.2",
    )
    silver_ds = lance.dataset(silver)
    assert silver_ds.data_storage_version == "2.2"
    table = silver_ds.to_table()
    assert all(len(thumb) > 0 for thumb in table.column("thumbnail").to_pylist())  # real thumbnails
    assert table.column("embedding")[0].as_py() != table.column("embedding")[1].as_py()  # per-image
    assert not blobs.is_blob_field(silver_ds.schema.field("thumbnail"))  # inline binary, not a blob
