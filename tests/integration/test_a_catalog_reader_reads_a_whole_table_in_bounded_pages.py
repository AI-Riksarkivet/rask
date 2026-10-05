"""A catalog reader with no limit reads the whole table, in pages the catalog's answer ceiling admits ([[LH-247]]).

`/query` refuses a `k` whose answer could exceed its ceiling, because the native backend builds each
answer whole in memory. `CatalogTableReader` is how the annotator reads in catalog mode
(`MEDIA_READ_BACKEND=catalog`), and with no limit it must still return every row: it pages with a
bounded `k` plus an offset, pinned to one version. Driven over a real socket because the reader's
transport is the generated urllib3 client, against the real catalog app on a real `dir` namespace.

The table is 2,000 rows of 4 KiB, so the catalog's ceiling is about 4,000 rows and the reader's pages
(64 rows, then about 1,000 at its measured width) take three answers to cover it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
import uvicorn

from service_kit.lancekit.reader import CatalogTableReader, RestCatalogTransport


_ROWS, _DIM = 2_000, 1024


@pytest.fixture
def catalog_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "RASK_OIDC_ENABLED": "false",
        "RASK_FGA_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(timeout=30)
    get_settings.cache_clear()


def _wide_rows() -> bytes:
    values = pa.array([0.25] * (_ROWS * _DIM), pa.float32())
    table = pa.table({"id": pa.array(range(_ROWS), pa.int64()), "vec": pa.FixedSizeListArray.from_arrays(values, _DIM)})
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def test_an_unlimited_read_returns_every_row(catalog_url: str) -> None:
    with httpx.Client(base_url=catalog_url, timeout=60) as client:
        assert client.post("/v1/namespace/paged/create", json={}).status_code == 200
        created = client.post("/v1/table/paged$t/create", content=_wide_rows(), headers={"content-type": "application/vnd.apache.arrow.stream"})
        assert created.status_code == 200, created.text

    table = CatalogTableReader(RestCatalogTransport(catalog_url), ["paged", "t"]).to_table(columns=["id", "vec"])

    assert sorted(table["id"].to_pylist()) == list(range(_ROWS))
