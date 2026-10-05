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

from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import lance
import pyarrow as pa
import pyarrow.ipc
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


def _valued(ids: list[int]) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64()), "v": pa.array([10 * i for i in ids], pa.int64())})


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


def _governed(catalog: TestClient, root: Path) -> str:
    """`db$t` at version 1 holding ids 1-3, with stable row ids."""
    uri = str(root / "t")
    lance.write_dataset(_valued([1, 2, 3]), uri, data_storage_version="2.2", enable_stable_row_ids=True)
    assert catalog.post("/v1/table/db$t/register", json={"location": "t"}).status_code == 200
    return uri


def _ids(resp: httpx.Response, uri: str) -> list[int]:
    """The ids a feed answer names: scanned rows carry them, and a deleted feed's `_rowid`s are resolved at version 1."""
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
    answered = pyarrow.ipc.open_file(pa.py_buffer(resp.content)).read_all()
    if "id" in answered.column_names:
        return sorted(answered.column("id").to_pylist())
    if answered.num_rows == 0:
        return []
    at_create = lance.dataset(uri, version=1).to_table(columns=["id"], with_row_id=True)
    id_of = dict(zip(at_create.column("_rowid").to_pylist(), at_create.column("id").to_pylist(), strict=True))
    return sorted(id_of[rowid] for rowid in answered.column("_rowid").to_pylist())


def test_a_closed_window_answers_the_same_set_after_the_table_moves_on(catalog: TestClient, root: Path) -> None:
    """A row updated inside the window and again after it still belongs to the window it changed in."""
    uri = _governed(catalog, root)
    lance.dataset(uri).update({"v": "v + 1"}, where="id = 1")  # v2, inside the window
    lance.dataset(uri).update({"v": "v + 1"}, where="id = 1")  # v3, the same row after it
    lance.dataset(uri).insert(_valued([4]))  # v4

    resp = catalog.post("/management/v1/table/db$t/changes", json={"begin_version": 1, "end_version": 2, "kind": "updated"})

    assert _ids(resp, uri) == [1], "the window was answered from the latest snapshot, where row 1 last changed at version 3"


def _restore(uri: str) -> None:
    lance.dataset(uri, version=1).restore()


def _overwrite(uri: str) -> None:
    lance.write_dataset(_valued([7]), uri, mode="overwrite")


def _update_columns(uri: str) -> None:
    """Rewrite `v` in place for one row: `fragment.update_columns` plus `LanceOperation.Update` (lance_docs/guide.md:1715-1770)."""
    dataset = lance.dataset(uri)
    updated, fields_modified = dataset.get_fragments()[0].update_columns(pa.table({"_rowid": pa.array([0], pa.uint64()), "v": pa.array([99], pa.int64())}))
    lance.LanceDataset.commit(uri, lance.LanceOperation.Update(updated_fragments=[updated], fields_modified=fields_modified), read_version=dataset.version)


def _dataset_update(uri: str) -> None:
    lance.dataset(uri).update({"v": "v + 1"}, where="id = 1")


def _merge_insert(uri: str) -> None:
    lance.dataset(uri).merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(_valued([2, 9]))


def _compaction(uri: str) -> None:
    lance.dataset(uri).delete("id = 3")
    lance.dataset(uri).optimize.compact_files(target_rows_per_fragment=1024)


def _branch_from_v1(uri: str) -> None:
    """A branch off version 2 that inserts and deletes; the window (1, head] on it starts below its branch point."""
    lance.dataset(uri).insert(_valued([4]))
    lance.dataset(uri).create_branch("dev", 2)
    lance.dataset(uri).checkout_version(("dev", None)).insert(_valued([5]))
    lance.dataset(uri).checkout_version(("dev", None)).delete("id = 2")


def _cleaned(uri: str) -> None:
    """Five versions with a delete at v4, then a cleanup that keeps only v4 and v5, as maintenance does."""
    lance.dataset(uri).insert(_valued([4]))
    lance.dataset(uri).insert(_valued([5]))
    lance.dataset(uri).delete("id = 2")
    lance.dataset(uri).insert(_valued([6]))
    lance.dataset(uri).cleanup_old_versions(retain_versions=2)


@pytest.mark.parametrize(
    ("change", "begin", "kind", "branch", "answer"),
    [
        pytest.param(_restore, 1, "updated", None, 409, id="restore"),
        pytest.param(_overwrite, 1, "inserted", None, 409, id="overwrite"),
        pytest.param(_update_columns, 1, "updated", None, 409, id="update_columns"),
        pytest.param(_dataset_update, 1, "updated", None, [1], id="dataset.update"),
        pytest.param(_merge_insert, 1, "updated", None, [2], id="merge_insert"),
        pytest.param(_compaction, 1, "deleted", None, [3], id="compaction"),
        pytest.param(_branch_from_v1, 1, "deleted", "dev", [2], id="branch-from-v1"),
        pytest.param(_cleaned, 0, "deleted", None, [], id="cleaned-resync-from-0"),
        pytest.param(_cleaned, 3, "deleted", None, 409, id="cleaned-begin-deleted"),
        pytest.param(_cleaned, 3, "updated", None, 409, id="cleaned-begin-updated"),
    ],
)
def test_a_window_is_answered_only_where_the_version_columns_describe_it(
    catalog: TestClient, root: Path, change: Callable[[str], None], begin: int, kind: str, branch: str | None, answer: int | list[int]
) -> None:
    """Restore, Overwrite and an in-place column rewrite are refused; every other window answers normally.

    Measured on pylance 12.0.0: `update_columns` commits an Update with `fields_modified=[<v>]` and
    leaves `_row_last_updated_at_version` unmoved, so its window would answer [] for a real change;
    `dataset.update` and `merge_insert` commit an Update with `fields_modified=[]`; a compaction is a
    ReserveFragments BaseOperation then a Rewrite; and `read_transaction` on a branch's first manifest
    panics. After a cleanup, a resynchronisation from 0 still answers, and a window whose begin manifest
    is gone is refused for every kind alike rather than answered by the scans and failed by `delta()`.
    """
    uri = _governed(catalog, root)
    change(uri)

    resp = catalog.post("/management/v1/table/db$t/changes", json={"begin_version": begin, "kind": kind, "branch": branch})

    if isinstance(answer, int):
        assert resp.status_code == answer, f"{resp.status_code}: {resp.text[:300]}"
        assert resp.json()["code"] == 19 and "begin_version=0" in resp.json()["detail"], resp.json()
    else:
        assert _ids(resp, uri) == answer
