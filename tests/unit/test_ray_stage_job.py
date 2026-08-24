"""The Ray stage job uses the SHARED primitives — it no longer carries copies to pin (B14).

This file used to be a drift-pin: the script inlined `_is_image`, `_derive_thumbnail`,
`_derive_embedding` and the blob-field pair, and these tests asserted each copy stayed
byte-identical to the services'. That is a test comparing two behaviours after the fact, which is
what B14 records as the WRONG fix — it detects divergence rather than preventing it, and only in the
cases someone thought to assert.

The copies are gone. `service_kit.lakehouse.media` and `service_kit.lakehouse.blobs` hold ONE
implementation each, and both drivers import them: the medallion service directly, and this script
too, because the Ray cluster image ships `service-kit` (it already imported `stamp_stage` from it).

So what is left to test is IDENTITY, not agreement: the script must reference the shared function
objects, and a future edit that reintroduces a local copy fails here. The behavioural tests for the
derivers themselves live with the implementation, where they belong.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import ModuleType

import pyarrow as pa
import pytest
from lance import blob_array, blob_field

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


def test_the_script_uses_the_shared_primitives_rather_than_copies() -> None:
    """Identity, not agreement — the property B14 asks for.

    A copy that merely *behaves* the same is what this file used to check. Asserting the script
    references the shared function objects means a reintroduced local copy fails immediately, rather
    than passing until it drifts in a way somebody remembered to assert.
    """
    from service_kit.lakehouse import media as shared_media
    from service_kit.lakehouse.blobs import blob_field_names as shared_blob_field_names

    job = _load_job()

    assert job.media is shared_media
    assert job.blob_field_names is shared_blob_field_names


def test_the_script_declares_no_local_deriver_copy() -> None:
    """The inlined names are GONE, not merely unused — a dormant copy is a copy."""
    job = _load_job()

    for gone in ("_derive_thumbnail", "_derive_embedding", "_is_image", "_open_guarded", "_is_blob_field"):
        assert not hasattr(job, gone), f"{gone} is back — B14 asks for one implementation, not two"
