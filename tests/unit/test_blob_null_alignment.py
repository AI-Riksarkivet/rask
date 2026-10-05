"""The R27 null-blob alignment guards — the class of defect that has no exception to catch it.

``read_blobs`` / ``take_blobs`` DROPPED null rows through pylance 9.0.0 (measured on 8.0.0 and 9.0.0;
FIXED in 10.0.0 — see ``docs/architecture/lance-blob-v2-findings.md``), so any code that paired their
output with a second scan
BY POSITION is misaligned the moment one row's payload is null — which is exactly the medallion's own
"a failed harvest, a skipped page" case. Before this guard the cascade turned one un-harvested page into
``ArrowInvalid: Column 1 named payload expected length 3 but got length 2``, which the stage runners classify as
a TRANSIENT failure: a RETRY storm re-reading every blob from S3 up to maxDeliver, then the DLQ, for a
condition redelivery can never fix.

Pinned here: the installed pylance's ``read_blobs`` and ``take_blobs`` keep a null row's slot, so a
regression upstream that restored the misalignment fails loudly.

Both engines carrying a null page through on its own row is pinned on each lane by
``tests/integration/test_both_engines_land_a_stage_through_one_write.py``.
"""

from __future__ import annotations

import io
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from lance import blob_array, blob_field


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
