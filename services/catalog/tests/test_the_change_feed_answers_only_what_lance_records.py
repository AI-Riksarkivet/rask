"""`POST /management/v1/table/{id}/changes` answers only what Lance's own row-version record supports.

Lance's change data feed is predicates over `_row_created_at_version` / `_row_last_updated_at_version`
plus the deleted-row record (`lance_docs/file_format.md:4270-4298`), and all three exist only where
the table has stable row ids (`file_format.md:4011-4015`). Measured on pylance 12.0.0: a table created
without them still answers both version columns, both read 1 for every row, and an `inserted` window
over a real append answers [] with a 200, the one answer a consumer cannot tell from "nothing changed".

Driven through the real catalog app over a real ``dir`` namespace. A table reaches the door without
stable row ids by being registered at an empty location and written there afterwards: the register
door judges the dataset it attaches, and there is none yet to judge.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "lance-catalog"
    path.mkdir()
    return path


@pytest.fixture
def catalog(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The catalog app as it boots, rooted on a local directory."""
    from catalog.core.config import get_settings

    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"file://{root}",
        "LANCE_CONTROL_ROOT": f"file://{tmp_path / 'control'}",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_S3_ACCESS_KEY_ID": "x",
        "LANCE_S3_SECRET_ACCESS_KEY": "x",
        "LANCE_S3_ENDPOINT_URL": "http://127.0.0.1:9",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    from catalog.main import app

    with TestClient(app) as client:
        assert client.post("/v1/namespace/db/create", json={}).status_code == 200
        yield client
    get_settings.cache_clear()


def _rows(ids: list[int]) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64())})


@pytest.mark.parametrize("kind", ["inserted", "deleted"])
def test_a_table_without_stable_row_ids_is_refused_rather_than_answered_empty(catalog: TestClient, root: Path, kind: str) -> None:
    """Both halves of the door: the scan predicates and the deleted-row record."""
    assert catalog.post("/v1/table/db$t/register", json={"location": "t"}).status_code == 200
    lance.write_dataset(_rows([1, 2]), str(root / "t"), data_storage_version="2.2")
    lance.write_dataset(_rows([3]), str(root / "t"), mode="append")
    lance.dataset(str(root / "t")).delete("id = 1")

    resp = catalog.post("/management/v1/table/db$t/changes", json={"begin_version": 1, "end_version": 3, "kind": kind})

    assert resp.status_code == 409, f"{resp.status_code}: {resp.text[:300]}"
    assert resp.json()["code"] == 19, "the spec's InvalidTableState code, which a generated client dispatches on"
    assert "stable row ids" in resp.json()["detail"]
