"""XC-104: no catalog write door lets a body become more than the catalog's body cap, and both insert arms agree.

The middleware counts the bytes sent; `LANCE_MAX_BODY_BYTES` is what the write load-shed assumes each write
holds. A body can outgrow that three ways after it is counted: by declaring values no byte backs, by
compressed buffers inflating as they are read, and by the insert coercion's cast decoding a dictionary once
per row (measured on pyarrow 25.0.0: a 656-byte insert cast to 1,004,000,296 bytes). Each is refused 400
code 13 before it is held, at every door and on both arms, with nothing written. Driven over HTTP against a
real `dir` backend with the cap at 1 MiB, which the middleware (sized at import) does not see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa
import pytest


if TYPE_CHECKING:
    from fastapi.testclient import TestClient


_CAP = 1024 * 1024
#: Every pool a test measures on, held for the process: a buffer allocated from one can outlive the
#: test (an app or Lance cache keeps it), and freed after its pool it dereferences freed memory, which
#: measured as a segfault of the test process.
_POOLS: list[pa.MemoryPool] = []


def _a_pool_of_its_own() -> pa.MemoryPool:
    pool = pa.proxy_memory_pool(pa.default_memory_pool())
    _POOLS.append(pool)
    return pool


ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}
INVALID_INPUT = 13


def _stream(table: pa.Table, compression: str | None = None) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema, options=pa.ipc.IpcWriteOptions(compression=compression)) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def capped(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> TestClient:
    """The real-backend client, with the catalog's cap set before its settings are first read."""
    monkeypatch.setenv("LANCE_MAX_BODY_BYTES", str(_CAP))
    client: TestClient = request.getfixturevalue("real_ns_client")
    assert client.post("/v1/namespace/db/create", json={}).status_code == 200
    seeded = client.post(
        "/v1/table/db$t/create", content=_stream(pa.table({"id": pa.array([1, 2, 3], pa.int64()), "s": ["a", "b", "c"]})), headers=ARROW_STREAM
    )
    assert seeded.status_code == 200, seeded.text
    assert client.post("/v1/table/db$t/branches/create", json={"name": "work"}).status_code == 200
    return client


def _rows(client: TestClient, table: str, branch: str | None) -> int:
    counted = client.post(f"/v1/table/{table}/count_rows", json={"branch": branch} if branch else {})
    assert counted.status_code == 200, counted.text
    return int(counted.text)


#: The table's own column names, so nothing but the bound can refuse it: cast to them, a million nulls are 16 MB.
_DECLARING = _stream(pa.table({"id": pa.nulls(10**6), "s": pa.nulls(10**6)}))
_INFLATING = _stream(pa.table({"id": pa.array([4, 5], pa.int64()), "s": ["x" * (2 * _CAP), "y" * (2 * _CAP)]}), "lz4")


@pytest.mark.parametrize(
    ("door", "branch", "body"),
    [
        pytest.param("db$new/create", None, _DECLARING, id="create-bytes-declaring-a-million-rows"),
        pytest.param("db$t/insert", None, _INFLATING, id="insert-main-an-lz4-body-inflating-to-4-MiB"),
        pytest.param("db$t/insert", "work", _DECLARING, id="insert-branch-bytes-declaring-a-million-rows"),
        pytest.param("db$t/merge_insert?on=id&when_not_matched_insert_all=true", None, _INFLATING, id="merge-main-an-lz4-body-inflating-to-4-MiB"),
        pytest.param("db$t/merge_insert?on=id&when_not_matched_insert_all=true", "work", _DECLARING, id="merge-branch-bytes-declaring-a-million-rows"),
    ],
)
def test_a_body_the_catalog_cannot_hold_is_refused_at_every_write_door(capped: TestClient, door: str, branch: str | None, body: bytes) -> None:
    assert len(body) < _CAP // 8, "the body is over the cap as sent, so this would not test what it becomes"
    separator = "&" if "?" in door else "?"
    query = f"{separator}branch={branch}" if branch else ""

    refused = capped.post(f"/v1/table/{door}{query}", content=body, headers=ARROW_STREAM)

    assert refused.status_code == 400, f"{refused.status_code} {refused.text[:300]}"
    assert refused.json().get("code") == INVALID_INPUT, refused.json()
    if door.endswith("/create"):
        assert capped.post("/v1/table/db$new/describe", json={}).status_code == 404, "a refused create left a table behind"
    else:
        assert _rows(capped, "db$t", branch) == 3, "a refused write must write no rows"


@pytest.mark.parametrize(("rows", "fits"), [pytest.param(50_000, True, id="widening-within-the-cap"), pytest.param(200_000, False, id="widening-past-the-cap")])
def test_a_widened_insert_answers_the_same_on_both_arms(capped: TestClient, rows: int, fits: bool) -> None:  # noqa: FBT001 — parametrized
    """int32 rows into an int64 column double as the table's types. The coercion holds that to the cap once,
    before either arm, so a branch cannot refuse what main accepts, nor main lose the count it reports."""
    assert capped.post("/v1/table/db$w/create", content=_stream(pa.table({"id": pa.array([0], pa.int64())})), headers=ARROW_STREAM).status_code == 200
    assert capped.post("/v1/table/db$w/branches/create", json={"name": "work"}).status_code == 200
    body = _stream(pa.table({"id": pa.array(range(rows), pa.int32())}))
    assert len(body) < _CAP, "the caller's body is over the cap as sent"

    answers = [capped.post(f"/v1/table/db$w/insert{query}", content=body, headers=ARROW_STREAM) for query in ("", "?branch=work")]

    if fits:
        assert [(a.status_code, a.json().get("num_inserted_rows")) for a in answers] == [(200, rows), (200, rows)], [a.text[:200] for a in answers]
    else:
        assert [a.status_code for a in answers] == [400, 400], [a.text[:200] for a in answers]
        assert all("as the table's column types" in a.json()["detail"] for a in answers), [a.json()["detail"] for a in answers]
        assert (_rows(capped, "db$w", None), _rows(capped, "db$w", "work")) == (1, 1)


@pytest.mark.parametrize("branch", [None], ids=["main"])
def test_a_dictionary_is_refused_before_the_cast_expands_it(capped: TestClient, branch: str | None) -> None:
    """5,000 rows referencing one 10 KiB value are ~60 KB sent and ~50 MB as the table's `string` column.
    Refused before the cast, the request allocates next to nothing: measured on a pool of its own."""
    label = pa.DictionaryArray.from_arrays(pa.array([0] * 5000, pa.int8()), pa.array(["x" * 10240]))
    body = _stream(pa.table({"id": pa.array(range(10, 5010), pa.int64()), "s": label}))
    pool, previous = _a_pool_of_its_own(), pa.default_memory_pool()
    pa.set_memory_pool(pool)
    try:
        refused = capped.post(f"/v1/table/db$t/insert{'?branch=' + branch if branch else ''}", content=body, headers=ARROW_STREAM)
    finally:
        pa.set_memory_pool(previous)

    assert refused.status_code == 400, refused.text[:300]
    assert "as the table's column types" in refused.json()["detail"], refused.json()
    assert pool.max_memory() < 8 * 1024 * 1024, f"the refusal allocated {pool.max_memory():,} bytes: the cast ran first"
    assert _rows(capped, "db$t", branch) == 3
