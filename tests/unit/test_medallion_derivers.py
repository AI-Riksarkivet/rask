"""Unit tests for content-dispatched artifact derivation — the multimodal contract (§9).

The contract under test: the pipeline operates on LANCE TYPES and dispatches derivation on blob
CONTENT — tabular datasets flow untouched, image blobs gain inline artifacts, unrecognised media
carries through intact — with zero media configuration anywhere. Real Lance datasets on tmp_path
(no fakes) so the blob-safe carry + the written schema are the real thing.
"""

from __future__ import annotations

import io
from pathlib import Path

import lance
import pyarrow as pa
from lance import blob_array, blob_field
from medallion.services import media
from medallion.services.compute import transform_stage
from medallion.services.derivers import derive_artifacts
from PIL import Image


def _png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _bronze_media(tmp_path: Path, payloads: list[bytes]) -> str:
    uri = str(tmp_path / "bronze_media")
    table = pa.table(
        {
            "id": pa.array(range(len(payloads)), pa.int64()),
            "payload": blob_array(payloads),
            "source_uri": pa.array([f"s3://src/{i}" for i in range(len(payloads))], pa.string()),
        },
        schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload"), pa.field("source_uri", pa.string())]),
    )
    lance.write_dataset(table, uri, mode="overwrite", data_storage_version="2.2")
    return uri


def test_image_blobs_derive_artifacts_through_the_generic_stage(tmp_path: Path) -> None:
    """The media lane needs NO special mover: the generic stage derives from image content."""
    bronze = _bronze_media(tmp_path, [_png((200, 40, 40)), _png((40, 40, 200))])
    silver = str(tmp_path / "silver_media")

    result = transform_stage(bronze, silver, {}, stage="silver-media")

    ds = lance.dataset(silver)
    assert set(ds.schema.names) == {
        "id",
        "payload",
        "source_uri",
        "stage",
        "source_rowid",  # row-level provenance minted at the head, carried through the media stage
        "thumbnail",
        "embedding",
    }
    thumbs = ds.to_table(columns=["thumbnail"]).column("thumbnail").to_pylist()
    assert all(t.startswith(b"\x89PNG") for t in thumbs)  # real derived thumbnails, inline
    embeddings = ds.to_table(columns=["embedding"]).column("embedding").to_pylist()
    assert all(len(e) == media.EMBEDDING_DIMS for e in embeddings)
    assert embeddings[0] != embeddings[1]  # pixel-derived: different images → different vectors
    # The emitted schema facet records the Lance types — what the lineage WROTE edge shows.
    facet_types = {f["name"]: f["type"] for f in result.fields}
    assert facet_types["payload"] == "blob"
    assert facet_types["embedding"].startswith("array")


def test_tabular_dataset_flows_untouched(tmp_path: Path) -> None:
    """Multimodal means tabular too: a no-blob dataset just moves — no probing, no artifacts."""
    src = str(tmp_path / "tabular")
    lance.write_dataset(
        pa.table({"id": pa.array([1, 2], pa.int64()), "v": pa.array([10, 20], pa.int64())}),
        src,
        data_storage_version="2.2",
    )
    dst = str(tmp_path / "tabular_out")

    transform_stage(src, dst, {}, stage="silver")

    assert set(lance.dataset(dst).schema.names) == {"id", "v", "stage", "source_rowid"}


def test_unrecognised_media_carries_through_untouched(tmp_path: Path) -> None:
    """A blob column is not necessarily an image (audio/docs): unknown content flows intact."""
    bronze = _bronze_media(tmp_path, [b"RIFF....WAVEfmt not-actually-parsed-audio"])
    silver = str(tmp_path / "silver_other")

    transform_stage(bronze, silver, {}, stage="silver")

    ds = lance.dataset(silver)
    names = set(ds.schema.names)
    assert "thumbnail" not in names and "embedding" not in names
    _addr, payload = ds.read_blobs("payload", indices=[0])[0]
    # pylance 10 types the payload `bytes | None`; losing the blob is exactly what this asserts
    # against, so the None case must fail loudly rather than be coerced.
    assert payload is not None and bytes(payload).startswith(b"RIFF")  # survived the hop byte-for-byte


def test_derive_skips_when_artifacts_already_present() -> None:
    """A later stage carries derived artifacts forward instead of re-deriving them."""
    table = pa.table({"id": pa.array([0], pa.int64()), "thumbnail": pa.array([b"t"], pa.large_binary())})
    assert derive_artifacts(table, {"payload": [_png((9, 9, 9))]}) is table


def test_derive_noop_on_empty_payloads() -> None:
    table = pa.table({"id": pa.array([], pa.int64())})
    assert derive_artifacts(table, {"payload": []}) is table


def test_blob_field_detection_keys_on_lance_type(tmp_path: Path) -> None:
    """Blob detection keys on the LANCE TYPE (blob column present), nothing else. (Was the Ray-path GATE
    until 2026-07-13 — the gate is gone now that the Ray stage job round-trips blobs; the detection still
    routes a media stage to the pylance blob path, in the job itself.)"""
    from service_kit.lakehouse import blobs

    blobby = _bronze_media(tmp_path, [_png((1, 2, 3))])
    tabular = str(tmp_path / "plain")
    lance.write_dataset(pa.table({"id": pa.array([1], pa.int64())}), tabular, data_storage_version="2.2")
    assert blobs.blob_field_names(lance.dataset(blobby).schema) == ["payload"]
    assert blobs.blob_field_names(lance.dataset(tabular).schema) == []
