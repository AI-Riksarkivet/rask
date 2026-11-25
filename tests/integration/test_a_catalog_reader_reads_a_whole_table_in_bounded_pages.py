"""A catalog reader with no limit reads the whole table, in pages the catalog's answer ceiling admits ([[LH-247]]).

`/query` refuses a `k` whose answer could exceed its ceiling, because the native backend builds each
answer whole in memory. `CatalogTableReader` reads a table through the catalog's doors, and with no
limit it must still return every row: it pages with a bounded `k` plus an offset, pinned to one
version. Driven over a real socket because the reader's transport is the generated urllib3 client,
against the real catalog app on a real `dir` namespace.

The table is 2,000 rows of 4 KiB, so the catalog's ceiling is about 4,000 rows and the reader's pages
(64 rows, then about 1,000 at its measured width) take three answers to cover it.
"""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pyarrow as pa
import pytest

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lineage.services.catalog_reader import CatalogTableReader, RestCatalogTransport
from tests.live_server import serve


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

    with serve(app) as url:
        yield url
    get_settings.cache_clear()


def _wide_rows() -> bytes:
    values = pa.array([0.25] * (_ROWS * _DIM), pa.float32())
    table = pa.table({"id": pa.array(range(_ROWS), pa.int64()), "vec": pa.FixedSizeListArray.from_arrays(values, _DIM)})
    return encode_arrow_stream(table)


def test_an_unlimited_read_returns_every_row(catalog_url: str) -> None:
    with httpx.Client(base_url=catalog_url, timeout=60) as client:
        assert client.post("/v1/namespace/lance_catalog$paged/create", json={}).status_code == 200
        created = client.post("/v1/table/lance_catalog$paged$t/create", content=_wide_rows(), headers={"content-type": ARROW_STREAM_MEDIA_TYPE})
        assert created.status_code == 200, created.text

    table = CatalogTableReader(RestCatalogTransport(catalog_url), ["lance_catalog", "paged", "t"]).to_table(columns=["id", "vec"])

    assert sorted(table["id"].to_pylist()) == list(range(_ROWS))
