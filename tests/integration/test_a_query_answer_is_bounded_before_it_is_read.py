"""`/query` and `analyze_plan` refuse a row count whose answer the catalog could not hold ([[LH-247]]).

The native backend materialises a query's whole answer in one buffer before the first byte is sent,
and the spec gives `k` a minimum of 0 and no maximum, where `k=0` means every row. MEASURED on pylance
12.0.0 from outside a one-CPU catalog process, on a 164 MB table of 1024-dim float32 vectors: `k=0`
returned 169 MB and peaked at 822,192 kB against the pod's 524,288 kB limit. `k` and `offset` above
u32 raised `OverflowError` inside the native call (500). Each is refused with code 13 before a row is
read; the served side of the bound, an answer at the ceiling staying under the memory limit, is pinned
by `test_a_wide_read_stays_under_the_catalog_memory_limit.py`.

The fixture is narrow in rows and wide in bytes: the ceiling is the response budget divided by the
projected row width, so 4 KiB rows put it at about four thousand, and the row count does not move it.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient


_ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
_DIM = 1024
_U32_OVER = 2**32


def _wide_table(rows: int) -> bytes:
    values = pa.array([0.5] * (rows * _DIM), pa.float32())
    table = pa.table({"id": pa.array(range(rows), pa.int64()), "vec": pa.FixedSizeListArray.from_arrays(values, _DIM)})
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.mark.parametrize(
    ("door", "body"),
    [
        pytest.param("query", {"k": 0, "vector": {}}, id="query-k0-every-row"),
        pytest.param("query", {"k": 100_000, "vector": {}}, id="query-k-above-the-ceiling"),
        pytest.param("query", {"k": _U32_OVER, "vector": {}}, id="query-k-above-u32"),
        pytest.param("query", {"k": 1, "offset": _U32_OVER, "vector": {}}, id="query-offset-above-u32"),
        pytest.param("analyze_plan", {"k": 0, "vector": {}}, id="analyze-k0-every-row"),
    ],
)
def test_an_unbounded_row_count_is_refused_as_invalid_input(real_ns_client: TestClient, door: str, body: dict[str, object]) -> None:
    assert real_ns_client.post("/v1/namespace/wide/create", json={}).status_code == 200
    created = real_ns_client.post("/v1/table/wide$t/create", content=_wide_table(8), headers=_ARROW)
    assert created.status_code == 200, created.text

    # The same app, answering a server fault as the 500 a caller would receive rather than re-raising it here.
    response = TestClient(real_ns_client.app, raise_server_exceptions=False).post(f"/v1/table/wide$t/{door}", json=body)

    problem = response.json() if response.headers["content-type"].startswith("application/problem+json") else {}
    assert (response.status_code, problem.get("code")) == (400, 13), response.text[:300]
