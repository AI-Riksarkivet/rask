"""A read whose answer is sized by the data costs the catalog a bounded amount of memory ([[LH-247]]).

Measured where it matters: a real catalog process under uvicorn, reading a 164 MB table of 1024-dim
float32 vectors from a moto store, its VmHWM read from OUTSIDE the process after the request. The
process is pinned to one CPU because the pod's limit is one CPU and Lance sizes its decode pool from
the CPUs it may run on; a host-sized pool would measure a process the estate never runs.

MEASURED on pylance 12.0.0 with Lance's scan defaults: the change feed over this table peaked at
640,196 kB, and the largest answer `/query` serves at 628,000 kB, against the pod's 524,288 kB limit;
a `k=0` query (every row) at 822,192 kB. With the catalog's bounds, 354,012 kB and 434,152 kB (an idle
catalog is about 271,000 kB).

The query case is an unindexed vector search, which reads the whole vector column whatever `k` is,
and it climbs `k` by doubling until the door refuses, so it measures the largest answer the door
actually serves rather than a number this test assumes the ceiling to be.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import boto3
import httpx
import lance
import numpy as np
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from lance_namespace import CreateNamespaceRequest, DeclareTableRequest, connect


#: The catalog's memory limit, `chart/values.yaml` (catalog resources.limits.memory: 512Mi).
_LIMIT_KB = 512 * 1024
_ROWS, _DIM = 40_000, 1024
_BUCKET = "wide-read"
_TABLE = "wide$t"
#: The table has no vector index, so every query is a flat search over the whole vector column: the most
#: a bounded `/query` can cost on it.
_PROBE = [0.5] * _DIM


@pytest.fixture(scope="module")
def wide_store(moto_url: str) -> str:
    """A moto store holding the 164 MB table, written as the catalog writes a table (2.2, stable row ids)."""
    boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1").create_bucket(Bucket=_BUCKET)
    props = {
        "root": f"s3://{_BUCKET}",
        "storage.endpoint": moto_url,
        "storage.access_key_id": "test",
        "storage.secret_access_key": "test",
        "storage.region": "us-east-1",
        "storage.allow_http": "true",
    }
    ns = connect("dir", props)
    ns.create_namespace(CreateNamespaceRequest(id=["wide"]))
    location = ns.declare_table(DeclareTableRequest(id=["wide", "t"])).location
    values = pa.array(np.random.default_rng(0).random(_ROWS * _DIM, dtype=np.float32))
    table = pa.table({"id": pa.array(range(_ROWS), pa.int64()), "vec": pa.FixedSizeListArray.from_arrays(values, _DIM)})
    storage = {key.removeprefix("storage."): value for key, value in props.items() if key.startswith("storage.")}
    lance.write_dataset(table, location, storage_options=storage, data_storage_version="2.2", enable_stable_row_ids=True, max_rows_per_file=10_000)
    return moto_url


@pytest.fixture
def catalog(wide_store: str, tmp_path: Path) -> Iterator[tuple[str, int]]:
    """A real catalog process on one CPU, as ``(base_url, pid)``."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AWS_", "OTEL_", "LANCE_"))} | {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{_BUCKET}",
        "LANCE_S3_ENDPOINT": wide_store,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "RASK_OIDC_ENABLED": "false",
        "RASK_FGA_ENABLED": "false",
        "RASK_INSECURE_ALLOW_UNAUTHENTICATED": "true",
    }
    log = (tmp_path / "catalog.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "catalog.main:app", "--port", str(port), "--log-level", "warning"], env=env, stdout=log, stderr=log
    )
    # Pinned before the interpreter has imported Lance, so every thread Lance starts inherits one CPU.
    os.sched_setaffinity(process.pid, {min(os.sched_getaffinity(0))})
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"the catalog did not start: {(tmp_path / 'catalog.log').read_text()[-2000:]}") from None
                time.sleep(0.2)
        yield f"http://127.0.0.1:{port}", process.pid
    finally:
        process.terminate()
        process.wait(timeout=30)
        log.close()


def _hwm_kb(pid: int) -> int:
    line = next(line for line in Path(f"/proc/{pid}/status").read_text().splitlines() if line.startswith("VmHWM:"))
    return int(line.split()[1])


def _rows(client: httpx.Client, path: str, body: dict[str, object]) -> tuple[int, int]:
    """``(status, rows)`` of an Arrow FILE answer; rows is 0 for a refusal."""
    response = client.post(path, json=body)
    if response.status_code != 200:
        return response.status_code, 0
    return 200, ipc.open_file(pa.py_buffer(response.content)).read_all().num_rows


@pytest.mark.parametrize("door", ["changes", "query"])
def test_a_wide_read_stays_under_the_catalog_memory_limit(catalog: tuple[str, int], door: str) -> None:
    base, pid = catalog
    with httpx.Client(base_url=base, timeout=300) as client:
        if door == "changes":
            served = [_rows(client, f"/management/v1/table/{_TABLE}/changes", {"begin_version": 0, "kind": "inserted"})]
            assert served == [(200, _ROWS)]
        else:
            served = []
            k = 1
            while k <= 2 * _ROWS:
                status, rows = _rows(client, f"/v1/table/{_TABLE}/query", {"k": k, "vector": {"single_vector": _PROBE}, "vector_column": "vec"})
                if status != 200:
                    assert status == 400
                    break
                served.append((k, rows))
                k *= 2
            assert served and served[-1][1] < _ROWS, f"the door served every row in one answer: {served}"

    assert _hwm_kb(pid) < _LIMIT_KB, f"the catalog peaked at {_hwm_kb(pid)} kB serving {door} ({served}), past its {_LIMIT_KB} kB limit"
