"""The R27 null-blob alignment guards — the class of defect that has no exception to catch it.

``read_blobs`` / ``take_blobs`` DROPPED null rows through pylance 9.0.0 (measured on 8.0.0 and 9.0.0;
FIXED in 10.0.0 — see ``docs/architecture/lance-blob-v2-findings.md``), so any code that paired their
output with a second scan
BY POSITION is misaligned the moment one row's payload is null — which is exactly the medallion's own
"a failed harvest, a skipped page" case. Before this guard the cascade turned one un-harvested page into
``ArrowInvalid: Column 1 named payload expected length 3 but got length 2``, which the movers classify as
a TRANSIENT failure: a RETRY storm re-reading every blob from S3 up to maxDeliver, then the DLQ, for a
condition redelivery can never fix.

Four things are pinned:

1. the LANDMINE ITSELF still exists in the installed pylance — if upstream ever fixes it this test fails
   loudly and the findings doc gets updated, rather than the note quietly rotting;
2. ``blobs.read_aligned_table`` is cardinality-preserving where ``read_blobs`` is not;
3. the in-process cascade (``compute.transform_stage``) carries a null page THROUGH — same row count,
   null payload, null artifacts, the tabular columns still on their own rows;
4. the Ray media path (``scripts/ray_stage_job.py::_media_transform``) produces the IDENTICAL table, so
   the two engines cannot disagree about a null page.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import ModuleType
from typing import Any

import lance
import pyarrow as pa
import pytest
from lance import blob_array, blob_field

from medallion.services import compute
from service_kit.lakehouse import blobs


_JOB_PATH = Path(__file__).parents[2] / "scripts" / "ray_stage_job.py"


def _load_job() -> ModuleType:
    """The Ray stage job is a standalone script (not an importable package) — load it by path."""
    spec = importlib.util.spec_from_file_location("ray_stage_job_null", _JOB_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _png(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (24, 18), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def bronze_with_a_null_page(tmp_path: Path) -> str:
    """A bronze page dataset shaped like the real IIIF head's, with page 1's harvest MISSING.

    Same columns the P7a ingest writes (``id``/``payload``/``source_uri``/``volume_id``/``page_key``) at
    the same format contract (2.2 + stable row ids), so the guard exercises the production schema.
    """
    uri = str(tmp_path / "bronze-pages.lance")
    schema = pa.schema(
        [
            pa.field("id", pa.int64()),
            blob_field("payload"),
            pa.field("source_uri", pa.string()),
            pa.field("volume_id", pa.string()),
            pa.field("page_key", pa.string()),
        ]
    )
    lance.write_dataset(
        pa.table(
            {
                "id": pa.array([0, 1, 2], pa.int64()),
                "payload": blob_array([_png((200, 30, 30)), None, _png((30, 30, 200))]),
                "source_uri": pa.array(["iiif://p0", "iiif://p1", "iiif://p2"]),
                "volume_id": pa.array(["A0060198"] * 3),
                "page_key": pa.array(["p0", "p1", "p2"]),
            },
            schema=schema,
        ),
        uri,
        mode="overwrite",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    return uri


def test_upstream_fixed_the_null_drop_and_the_shape_changed_with_it(bronze_with_a_null_page: str) -> None:
    """THE LANDMINE IS FIXED UPSTREAM, as of pylance 10.0.0 — and this test firing is how we found out.

    It used to assert the opposite (`== 2` for three selected rows) as the premise every guard in this
    file rests on, with an explicit note that a pass-to-fail flip would be "the signal to re-measure and
    update docs/architecture/lance-blob-v2-findings.md". The 9.0.0 -> 10.0.0 upgrade flipped it on
    2026-08-16 and the doc was updated. Keeping the tripwire pointed the other way matters just as much:
    a regression upstream would silently restore the misalignment this file exists to prevent.

    Both entry points now preserve cardinality, and `read_blobs` ALSO changed shape — it yields
    `(row_index, bytes)` tuples where it used to yield blob handles, so anything calling `.size()` or
    `.read_range()` on its output breaks. `take_blobs` still yields handles but now puts `None` in a
    null row's slot instead of omitting the row.
    """
    ds = lance.dataset(bronze_with_a_null_page)
    assert ds.count_rows() == 3

    read = list(ds.read_blobs("payload", indices=[0, 1, 2]))
    assert len(read) == 3, "cardinality is preserved as of pylance 10 — one entry per selected row"
    assert all(isinstance(entry, tuple) and len(entry) == 2 for entry in read), "read_blobs now yields (row_index, bytes) tuples, not blob handles"

    took = ds.take_blobs("payload", indices=[0, 1, 2])
    assert len(took) == 3, "take_blobs no longer omits the null row"
    assert took[1] is None, "the null payload is None in its slot, not absent"
    assert took[0] is not None and took[2] is not None


def test_read_aligned_table_preserves_cardinality(bronze_with_a_null_page: str) -> None:
    """``blobs.read_aligned_table`` returns one row per selected row, the null payload as ``None``."""
    ds = lance.dataset(bronze_with_a_null_page)
    table = blobs.read_aligned_table(ds, columns=["id", "payload", "page_key"], with_row_id=True)
    assert table.num_rows == 3
    payloads = table.column("payload").to_pylist()
    assert payloads[1] is None
    assert payloads[0] is not None and payloads[2] is not None
    # the tabular columns are still on their own rows — the whole point of alignment
    assert table.column("page_key").to_pylist() == ["p0", "p1", "p2"]
    assert "_rowid" in table.column_names


def test_read_aligned_table_payloads_round_trip_through_blob_array(bronze_with_a_null_page: str) -> None:
    """The aligned payload list is directly re-wrappable as a blob column (nulls survive the write)."""
    ds = lance.dataset(bronze_with_a_null_page)
    payloads = blobs.read_aligned_table(ds, columns=["payload"]).column("payload").to_pylist()
    out_uri = bronze_with_a_null_page.replace("bronze-pages", "rewrapped")
    lance.write_dataset(
        pa.table({"payload": blob_array(payloads)}, schema=pa.schema([blob_field("payload")])),
        out_uri,
        mode="overwrite",
        data_storage_version="2.2",
    )
    again = blobs.read_aligned_table(lance.dataset(out_uri), columns=["payload"]).column("payload").to_pylist()
    assert [p is None for p in again] == [False, True, False]


def test_the_cascade_carries_a_null_page_through(bronze_with_a_null_page: str, tmp_path: Path) -> None:
    """A missing page no longer fails the stage: 3 rows out, null payload, null artifacts, rows aligned.

    This is the regression: on the pre-fix compute this raised
    ``ArrowInvalid: Column 1 named payload expected length 3 but got length 2``.
    """
    silver = str(tmp_path / "silver-pages.lance")
    result = compute.transform_stage(bronze_with_a_null_page, silver, {}, stage="silver")
    assert result.row_count == 3

    ds = lance.dataset(silver)
    table = blobs.read_aligned_table(ds, columns=["id", "payload", "page_key", "thumbnail", "embedding", "stage"])
    assert table.column("page_key").to_pylist() == ["p0", "p1", "p2"]
    assert [p is None for p in table.column("payload").to_pylist()] == [False, True, False]
    # the image deriver ran for the present pages and emitted NULL — not a shifted value — for the absent one
    assert [t is None for t in table.column("thumbnail").to_pylist()] == [False, True, False]
    assert [e is None for e in table.column("embedding").to_pylist()] == [False, True, False]
    assert set(table.column("stage").to_pylist()) == {"silver"}


def test_the_ray_media_path_agrees_with_the_in_process_path(bronze_with_a_null_page: str, tmp_path: Path) -> None:
    """``ray_stage_job._media_transform`` writes the same table the in-process cascade does.

    The two engines derive the same artifacts by construction (the deriver primitives are drift-pinned by
    test_ray_stage_job.py); this pins the NULL contract too, so a page can never be present on one engine
    and shifted on the other.
    """
    job = _load_job()
    in_process = str(tmp_path / "silver-in-process.lance")
    on_ray = str(tmp_path / "silver-on-ray.lance")
    compute.transform_stage(bronze_with_a_null_page, in_process, {}, stage="silver")
    job._media_transform(bronze_with_a_null_page, on_ray, {}, stage="silver")

    columns = ["id", "payload", "source_uri", "volume_id", "page_key", "thumbnail", "embedding", "stage", "source_rowid"]
    left: dict[str, Any] = blobs.read_aligned_table(lance.dataset(in_process), columns=columns).to_pydict()
    right: dict[str, Any] = blobs.read_aligned_table(lance.dataset(on_ray), columns=columns).to_pydict()
    assert left == right
