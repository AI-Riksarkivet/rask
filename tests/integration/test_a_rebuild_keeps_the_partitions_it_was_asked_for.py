"""A rebuild asks Lance for the partitions an earlier rebuild was asked for, not for what Lance built ([[LH-105]]).

From pylance 13.0.0 a vector index on a table too small to train is built with fewer IVF partitions than
requested (lance #9134: 512 rows asking 4 build 2), and the readback reports what was built. Without the
request kept, the next rebuild on the grown table reads back 2 and asks for 2, so the operator's 4 is lost
and nothing says so.

Driven through the real catalog app on real pylance: two `maintenance/reindex` calls with an append between
them, and the partitions read off the index Lance built.
"""

from __future__ import annotations

import io
import json

import lance
import numpy as np
import pyarrow as pa
import pyarrow.ipc as ipc
from fastapi.testclient import TestClient


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
TABLE = "lh105$t"
DIMENSION = 8


def _ipc(rows: int, seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    table = pa.table({"vec": pa.FixedSizeListArray.from_arrays(pa.array(rng.random(rows * DIMENSION, dtype="float32")), DIMENSION)})
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _partitions(location: str) -> int:
    return int(json.loads(lance.dataset(location)._ds.index_statistics("vec_idx"))["indices"][0]["num_partitions"])


def test_a_rebuild_on_a_grown_table_asks_for_the_partitions_requested_before(real_ns_client: TestClient) -> None:
    assert real_ns_client.post("/v1/namespace/lh105/create", json={}).status_code == 200
    created = real_ns_client.post(f"/v1/table/{TABLE}/create?mode=overwrite", content=_ipc(512, seed=1), headers=ARROW)
    assert created.status_code == 200, created.text
    location = str(real_ns_client.post(f"/v1/table/{TABLE}/describe", json={}).json()["table_uri"])
    lance.dataset(location).create_index("vec", index_type="IVF_PQ", name="vec_idx", num_partitions=2, num_sub_vectors=2)
    asked = real_ns_client.post(f"/management/v1/table/{TABLE}/maintenance/reindex", json={"index_name": "vec_idx", "params": {"num_partitions": 4}})
    assert asked.status_code == 200, asked.text
    assert _partitions(location) == 2, "the table trained 4 partitions, so it cannot show Lance building fewer than asked"
    grown = real_ns_client.post(f"/v1/table/{TABLE}/insert?mode=append", content=_ipc(1024, seed=2), headers=ARROW)
    assert grown.status_code == 200, grown.text

    rebuilt = real_ns_client.post(f"/management/v1/table/{TABLE}/maintenance/reindex", json={"index_name": "vec_idx"})

    assert rebuilt.status_code == 200, rebuilt.text
    assert _partitions(location) == 4
