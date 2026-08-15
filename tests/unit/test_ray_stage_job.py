"""Drift-pin for the Ray stage job's INLINED blob/deriver primitives.

``scripts/ray_stage_job.py`` is a standalone script baked into the Ray image (no ``services/`` imports —
lance_ray is Ray-image-only), so for the MEDIA path it INLINES the blob-v2 detection + the thumbnail /
embedding derivation instead of importing ``service_kit.lakehouse.blobs`` / ``medallion.services.media``. That is the
repo's self-contained-ray-job convention (``ray_train_job`` inlines ``run_id_for`` the same way).

Inlining is only safe if it CANNOT DRIFT from the in-process path — otherwise a media dataset would get a
different thumbnail/embedding depending on whether the stage ran on Ray or in-process. This test pins the
inlined mirrors byte-for-byte against the authoritative ``services`` implementations, so any change to one
side without the other fails here. It also confirms *why* the inline exists is real, not assumed:
medallion-producer strips blob-v2 typing on read at every version we have pinned — 0.4.2 (verified live 2026-07-13)
and 0.5.0, whose datasource says so in the source: "Blob v2 extension columns are exposed as plain
LargeBinary bytes" (R27, 2026-07-28). The Ray path must therefore re-wrap via
pylance), and the deriver is our business logic, never something medallion-producer provides.

Loaded via ``spec_from_file_location`` (the script isn't an importable package); lance_ray is imported
lazily inside ``main`` so the module — hence these primitives — loads cleanly in the unit venv.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import ModuleType

import pyarrow as pa
import pytest
from lance import blob_array, blob_field
from medallion.services import media

from service_kit.lakehouse import blobs


_JOB_PATH = Path(__file__).parents[2] / "scripts" / "ray_stage_job.py"


def _load_job() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ray_stage_job", _JOB_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


job = _load_job()


@pytest.fixture(scope="module")
def png_bytes() -> bytes:
    """A tiny real PNG (needs Pillow, which the unit venv has via the deriver deps)."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (123, 200, 50)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_inlined_thumbnail_matches_services(png_bytes: bytes) -> None:
    """The Ray job's ``_derive_thumbnail`` is byte-identical to ``media.derive_thumbnail``."""
    assert job._derive_thumbnail(png_bytes) == media.derive_thumbnail(png_bytes)


def test_inlined_embedding_matches_services(png_bytes: bytes) -> None:
    """The Ray job's ``_derive_embedding`` is identical to ``media.derive_embedding`` (same dims + values)."""
    assert job._derive_embedding(png_bytes) == media.derive_embedding(png_bytes)
    assert len(job._derive_embedding(png_bytes)) == media.EMBEDDING_DIMS == job._EMBEDDING_DIMS


def test_inlined_is_image_matches_services(png_bytes: bytes) -> None:
    """The Ray job's ``_is_image`` agrees with ``media.is_image`` on both an image and non-image bytes."""
    assert job._is_image(png_bytes) is media.is_image(png_bytes) is True
    assert job._is_image(b"not-an-image") is media.is_image(b"not-an-image") is False


def test_inlined_blob_detection_matches_common() -> None:
    """The Ray job's ``_is_blob_field`` / ``_blob_field_names`` agree with ``service_kit.lakehouse.blobs`` on a real schema.

    Built with the same ``blob_field`` the ingest uses, so a blob column is detected identically on the Ray
    path and the in-process path — the gate that sends a media stage down the pylance round-trip."""
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("payload"), pa.field("uri", pa.string())])
    assert job._blob_field_names(schema) == blobs.blob_field_names(schema) == ["payload"]
    for field in schema:
        assert job._is_blob_field(field) == blobs.is_blob_field(field)
    # And the constant the detection keys on hasn't drifted from service_kit.lakehouse.blobs.
    assert job._BLOB_V2_EXTENSION_NAME == blobs.BLOB_V2_EXTENSION_NAME


def test_media_transform_round_trips_blob_and_derives(tmp_path: Path, png_bytes: bytes) -> None:
    """End-to-end: ``_media_transform`` preserves blob-v2 typing AND appends thumbnail+embedding.

    This is the whole point of the Ray blob path — a plain lance_ray write would demote ``payload`` to
    LargeBinary; the pylance round-trip keeps it a blob-v2 column and derives the image artifacts, exactly
    like the in-process ``compute.transform_stage``.
    """
    import lance

    src = str(tmp_path / "bronze_media")
    table = pa.table(
        {"id": pa.array([0, 1], pa.int64()), "payload": blob_array([png_bytes, png_bytes])},
        schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload")]),
    )
    lance.write_dataset(table, src, data_storage_version="2.2", enable_stable_row_ids=True)
    src_rowids = lance.dataset(src).to_table(with_row_id=True).column("_rowid").to_pylist()

    dst = str(tmp_path / "silver_media")
    job._media_transform(src, dst, {}, stage="silver-media")

    out = lance.dataset(dst)
    names = out.schema.names
    assert "thumbnail" in names and "embedding" in names  # image artifacts derived
    assert "stage" in names
    assert blobs.blob_field_names(out.schema) == ["payload"]  # blob-v2 typing PRESERVED (not demoted)
    assert out.count_rows() == 2 and out.has_stable_row_ids
    # Row-level provenance parity with compute._carry_forward: source_rowid minted from the source _rowid.
    assert out.to_table(columns=["source_rowid"]).column("source_rowid").to_pylist() == src_rowids


def test_stamp_stage_mints_source_rowid_at_the_head_and_carries_it_forward() -> None:
    """The tabular map function's provenance logic (the distributed path can't run in the unit venv).

    Head: a batch carrying the reserved ``_rowid`` metacolumn (no ``source_rowid`` yet) MINTS source_rowid
    from it and never persists ``_rowid``. Carried: a batch that already has ``source_rowid`` keeps it
    UNCHANGED (root provenance, not re-set to the parent's _rowid) and still sheds ``_rowid``. Mirrors
    compute._carry_source_rowid + _stamp_stage.
    """
    head = pa.table({"id": [1, 2], "_rowid": pa.array([40, 41], pa.uint64())})
    stamped = job._stamp_stage(head, "bronze")
    assert "_rowid" not in stamped.column_names  # reserved metacolumn never persisted
    assert stamped.column("source_rowid").to_pylist() == [40, 41]  # minted from _rowid
    assert stamped.column("stage").to_pylist() == ["bronze", "bronze"]

    carried = pa.table(
        {
            "id": [1, 2],
            "source_rowid": pa.array([40, 41], pa.uint64()),
            "_rowid": pa.array([7, 8], pa.uint64()),
        }
    )
    stamped2 = job._stamp_stage(carried, "silver")
    assert "_rowid" not in stamped2.column_names
    assert stamped2.column("source_rowid").to_pylist() == [40, 41]  # ROOT id kept, NOT the parent's _rowid


def test_stamp_stage_re_stamps_the_lineage_column_instead_of_inheriting_it() -> None:
    """R26 on the DISTRIBUTED path: the job writes its own provenance document, never the parent's.

    Parity with compute._drop_inherited_lineage + the in-table stamp — the Ray path must not produce a
    governed dataset the in-process path would have stamped, and an inherited cell would label gold rows
    with silver's run. Empty ``lineage`` (the job run by hand) drops the column and adds none.
    """
    doc = '{"run_id": "r-1", "operation": "aggregate_gold"}'
    parent = '{"run_id": "r-0", "operation": "embed_features"}'
    upstream = pa.table(
        {
            "id": [1, 2],
            "source_rowid": pa.array([40, 41], pa.uint64()),
            "lineage": pa.array([parent, parent], pa.json_()),
        }
    )

    stamped = job._stamp_stage(upstream, "gold", doc)
    assert stamped.column_names.count("lineage") == 1
    assert stamped.schema.field("lineage").type.extension_name == "arrow.json"  # Lance JSONB, not a string
    assert stamped.column("lineage").to_pylist() == [doc, doc]  # THIS run's document, on every row

    bare = job._stamp_stage(upstream, "gold")
    assert "lineage" not in bare.column_names  # no document handed over → the parent's is still dropped


def test_media_transform_stamps_the_lineage_column(tmp_path: Path, png_bytes: bytes) -> None:
    """R26 on the Ray MEDIA path: the blob round-trip writes the provenance column in its own commit."""
    import lance

    src = str(tmp_path / "src")
    table = pa.table(
        {"id": pa.array([1, 2], pa.int64()), "payload": blob_array([png_bytes, png_bytes])},
        schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload")]),
    )
    lance.write_dataset(table, src, mode="overwrite", data_storage_version="2.2", enable_stable_row_ids=True)
    doc = '{"run_id": "r-9", "operation": "aggregate_gold"}'

    job._media_transform(src, str(tmp_path / "dst"), {}, stage="gold", lineage=doc)

    out = lance.dataset(str(tmp_path / "dst"))
    assert out.schema.field("lineage").type.extension_name == "arrow.json"
    assert out.to_table(columns=["id"], filter="json_get_string(lineage, 'run_id') = 'r-9'").num_rows == 2
