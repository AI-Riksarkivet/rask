"""The insert and merge_insert doors refuse a body that is not a valid Arrow IPC stream, on both arms.

A body's framing can parse while its buffers lie: offsets past the values buffer, offsets that
decrease, string values that are not UTF-8. Decoded by pyarrow and handed to Lance in-process — the
branch arm of both doors — the first is written as bytes from outside the request (measured on
pylance 12.0.0: `?branch=work` answered 200 and the branch held a 65,531-byte value from a 2-row,
10-byte body). Main hands the bytes to the native backend, whose reader refuses the same body as a 500.

So both doors decode through `dataplane.read_arrow_body`, which validates the buffers in full, and every
such body answers 400 code 13 with nothing written. Driven over HTTP against a real `dir` backend.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from lance_namespace import InsertIntoTableRequest, InvalidInputError, connect

from catalog.services.dataplane import create_table, insert_into_table, open_dataset


if TYPE_CHECKING:
    from pathlib import Path

    from fastapi.testclient import TestClient

ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}
INVALID_INPUT = 13
TABLE = "db$t"


def _stream(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


def _table(ids: list[int], blobs: list[bytes], texts: list[str]) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64()), "v": pa.array(blobs, pa.binary()), "s": pa.array(texts, pa.string())})


def _tampered(good: bytes, bad: bytes) -> bytes:
    """Two new rows whose stream still frames and reads after `good` is overwritten with `bad`."""
    body = _stream(_table([10, 11], [b"hello", b"world"], ["qq", "zz"]))
    assert body.count(good) == 1, "the bytes to tamper with are not unique in the stream"
    return body.replace(good, bad)


_BINARY_OFFSETS = struct.pack("<iii", 0, 5, 10)

BODIES = [
    pytest.param(b"this is not an arrow ipc stream", id="a-body-that-is-not-arrow"),
    pytest.param(_stream(_table([10, 11], [b"hello", b"world"], ["qq", "zz"]))[:-20], id="a-stream-cut-inside-its-batch"),
    pytest.param(_tampered(_BINARY_OFFSETS, struct.pack("<iii", 0, 5, 65536)), id="binary-offsets-past-the-values-buffer"),
    pytest.param(_tampered(_BINARY_OFFSETS, struct.pack("<iii", 0, 8, 5)), id="binary-offsets-that-decrease"),
    pytest.param(_tampered(b"qqzz", b"\xff\xfezz"), id="utf8-values-that-are-not-utf8"),
]


def _seeded(client: TestClient, branch: str | None) -> None:
    assert client.post("/v1/namespace/db/create", json={}).status_code == 200
    created = client.post(f"/v1/table/{TABLE}/create", content=_stream(_table([1, 2, 3], [b"a", b"b", b"c"], ["x", "y", "z"])), headers=ARROW_STREAM)
    assert created.status_code == 200, created.text
    if branch is not None:
        made = client.post(f"/v1/table/{TABLE}/branches/create", json={"name": branch})
        assert made.status_code == 200, made.text


def _rows_on(client: TestClient, branch: str | None) -> int:
    counted = client.post(f"/v1/table/{TABLE}/count_rows", json={"branch": branch} if branch else {})
    assert counted.status_code == 200, counted.text
    return int(counted.text)


@pytest.mark.parametrize("body", BODIES)
@pytest.mark.parametrize("branch", [None, "work"], ids=["main", "branch"])
@pytest.mark.parametrize("door", ["insert", "merge_insert?on=id&when_not_matched_insert_all=true"], ids=["insert", "merge_insert"])
def test_a_write_body_that_is_not_a_valid_arrow_stream_is_refused_and_writes_nothing(
    real_ns_client: TestClient, door: str, branch: str | None, body: bytes
) -> None:
    _seeded(real_ns_client, branch)
    separator = "&" if "?" in door else "?"
    query = f"{separator}branch={branch}" if branch else ""

    refused = real_ns_client.post(f"/v1/table/{TABLE}/{door}{query}", content=body, headers=ARROW_STREAM)

    assert refused.status_code == 400, f"the malformed body was not refused 400: {refused.status_code} {refused.text[:300]}"
    assert refused.json().get("code") == INVALID_INPUT, refused.json()
    assert "not an Arrow IPC stream" in str(refused.json().get("detail")), refused.json()
    assert _rows_on(real_ns_client, branch) == 3, "a refused write must write no rows"


@pytest.mark.parametrize("body", BODIES)
def test_the_insert_branch_arm_refuses_it_without_the_door(tmp_path: Path, body: bytes) -> None:
    """`dataplane.insert_into_table` is a seam of its own: its branch arm must not depend on the door's
    coercion having read the body first, because it is the arm that hands the buffers to Lance."""
    ns = connect("dir", {"root": str(tmp_path / "data")})
    create_table(ns, {}, ["t"], _table([1, 2, 3], [b"a", b"b", b"c"], ["x", "y", "z"]), mode="create")
    open_dataset(ns, {}, ["t"]).create_branch("work", None)

    with pytest.raises(InvalidInputError, match="not an Arrow IPC stream"):
        insert_into_table(ns, {}, InsertIntoTableRequest(id=["t"], branch="work"), body)

    assert open_dataset(ns, {}, ["t"], branch="work").count_rows() == 3, "a refused insert must write no rows"


@pytest.mark.parametrize("branch", [None, "work"], ids=["main", "branch"])
@pytest.mark.parametrize("door", ["insert", "merge_insert?on=id&when_not_matched_insert_all=true"], ids=["insert", "merge_insert"])
def test_a_valid_body_still_writes(real_ns_client: TestClient, door: str, branch: str | None) -> None:
    """The positive half: the same two rows, untampered, land on the ref the request names."""
    _seeded(real_ns_client, branch)
    separator = "&" if "?" in door else "?"
    query = f"{separator}branch={branch}" if branch else ""

    written = real_ns_client.post(
        f"/v1/table/{TABLE}/{door}{query}", content=_stream(_table([10, 11], [b"hello", b"world"], ["qq", "zz"])), headers=ARROW_STREAM
    )

    assert written.status_code == 200, written.text
    assert _rows_on(real_ns_client, branch) == 5
