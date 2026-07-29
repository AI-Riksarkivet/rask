"""End-to-end tests against a running server (full Docker stack on MinIO).

Set ``LANCE_REST_E2E_URL`` (e.g. http://localhost:2333) to run; skipped otherwise.
"""

from __future__ import annotations

import os

import pyarrow as pa
import pytest
import requests


URL = os.environ.get("LANCE_REST_E2E_URL", "")
ARROW = {"content-type": "application/vnd.apache.arrow.stream"}

pytestmark = pytest.mark.e2e


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture(scope="module")
def base() -> str:
    if not URL:
        pytest.skip("set LANCE_REST_E2E_URL to run e2e tests")
    try:
        requests.get(f"{URL}/livez", timeout=5).raise_for_status()
    except Exception:
        pytest.skip(f"server not reachable at {URL}")
    return URL.rstrip("/")


def test_full_lifecycle(base: str) -> None:
    ns, table = "e2e_ns", "e2e_ns$t"
    # clean slate
    requests.post(f"{base}/v1/table/{table}/drop")
    requests.post(f"{base}/v1/namespace/{ns}/drop")

    assert requests.post(f"{base}/v1/namespace/{ns}/create", json={}).status_code == 200
    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "name": ["a", "b", "c"]})
    assert requests.post(f"{base}/v1/table/{table}/create?mode=overwrite", data=_ipc(rows), headers=ARROW).status_code == 200

    assert (
        requests.post(
            f"{base}/v1/table/{table}/insert?mode=append",
            data=_ipc(pa.table({"id": pa.array([4], pa.int64()), "name": ["d"]})),
            headers=ARROW,
        ).status_code
        == 200
    )
    assert int(requests.post(f"{base}/v1/table/{table}/count_rows", json={}).text) == 4

    query = requests.post(f"{base}/v1/table/{table}/query", json={"k": 10, "filter": "id >= 2", "vector": {}})
    assert query.headers["content-type"].startswith("application/vnd.apache.arrow.file")
    assert pa.ipc.open_file(pa.BufferReader(query.content)).read_all().num_rows == 3

    assert requests.post(f"{base}/v1/table/{table}/update", json={"predicate": "id = 1", "updates": [["name", "'X'"]]}).status_code == 200
    assert requests.post(f"{base}/v1/table/{table}/tags/create", json={"tag": "v1", "version": 1}).status_code == 200

    # cleanup
    assert requests.post(f"{base}/v1/table/{table}/drop").status_code == 200
    assert requests.post(f"{base}/v1/namespace/{ns}/drop").status_code == 200


def test_unsupported_is_501(base: str) -> None:
    resp = requests.post(f"{base}/v1/materialized_view/x$mv/refresh", json={})
    assert resp.status_code == 501
