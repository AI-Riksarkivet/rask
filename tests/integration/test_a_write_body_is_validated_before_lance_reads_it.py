"""The insert and merge_insert doors refuse a body that is not a valid Arrow IPC stream, on both arms.

A body's framing can parse while its buffers lie: offsets past the values buffer, offsets that
decrease, string values that are not UTF-8. Decoded by pyarrow and handed to Lance in-process — the
branch arm of both doors — the first is written as bytes from outside the request (measured on
pylance 12.0.0: `?branch=work` answered 200 and the branch held a 65,531-byte value from a 2-row,
10-byte body). Main hands the bytes to the native backend, whose reader refuses the same body as a 500.

Other bodies make the reader raise a class of its own — an unknown dictionary id (KeyError), a
compressed buffer declaring 2**50 bytes (MemoryError), a 4-bit integer (NotImplementedError) — and
reading any body runs each registered extension type's deserializer on the caller's metadata, which
raises what that library raises: `import lance` registers `lance.blob.v2`, whose deserializer raises
TypeError for a storage type it refuses. Lance refuses metadata that is not UTF-8 with an untyped
ValueError on write (pylance 12.0.0).

So both doors decode through `dataplane.read_arrow_body`, over the fleet's one validating decoder in
`service_kit.lancekit.arrow_ipc`, and every such body answers 400 code 13 with nothing written. Driven
over HTTP against a real `dir` backend.
"""

from __future__ import annotations

import struct
from decimal import Decimal
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


_END_OF_STREAM = b"\xff\xff\xff\xff\x00\x00\x00\x00"


def _messages(body: bytes) -> list[bytes]:
    return [message.serialize().to_pybytes() for message in ipc.MessageReader.open_stream(body)]


def _a_dictionary_batch_whose_id_names_no_field() -> bytes:
    """A one-dictionary schema followed by the dictionary batch another stream wrote for its id 1."""
    one = _messages(_stream(pa.table({"id": pa.array([10], pa.int64()), "d": pa.array(["cat"]).dictionary_encode()})))
    two = _messages(_stream(pa.table({"a": pa.array(["cat"]).dictionary_encode(), "b": pa.array(["dog"]).dictionary_encode()})))
    return b"".join([one[0], two[2], *one[1:], _END_OF_STREAM])


def _a_compressed_text_declaring(declared: int, codec: str) -> bytes:
    """The `s` values buffer's 8-byte uncompressed-length prefix rewritten, so the reader allocates `declared`."""
    table = pa.table({"id": pa.array([10], pa.int64()), "s": ["x" * 1003]})
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema, options=ipc.IpcWriteOptions(compression=codec)) as writer:
        writer.write_table(table)
    body = sink.getvalue().to_pybytes()
    at = body.rindex(struct.pack("<q", 1003))
    return body[:at] + struct.pack("<q", declared) + body[at + 8 :]


def _an_integer_narrower_than_eight_bits() -> bytes:
    """An int16 column whose schema declares a 4-bit width, at the byte where int16 and int32 schemas differ."""
    narrow, wide = (_stream(pa.table({"id": pa.array([10], pa.int64()), "n": pa.array([1], kind)})) for kind in (pa.int16(), pa.int32()))
    at = next(i for i, (a, b) in enumerate(zip(narrow, wide, strict=True)) if a != b)
    return narrow[:at] + bytes([4]) + narrow[at + 1 :]


def _blob_named_on(table: pa.Table, column: str) -> bytes:
    """`table` whose `column` names pylance's `lance.blob.v2`, which `import lance` registers and whose deserializer refuses any storage but its struct."""
    schema = table.schema
    at = schema.get_field_index(column)
    named = schema.field(at).with_metadata({b"ARROW:extension:name": b"lance.blob.v2", b"ARROW:extension:metadata": b""})
    return _stream(table.cast(schema.set(at, named)))


_BINARY_OFFSETS = struct.pack("<iii", 0, 5, 10)

BODIES = [
    pytest.param(b"this is not an arrow ipc stream", id="a-body-that-is-not-arrow"),
    pytest.param(_stream(_table([10, 11], [b"hello", b"world"], ["qq", "zz"]))[:-20], id="a-stream-cut-inside-its-batch"),
    pytest.param(_tampered(_BINARY_OFFSETS, struct.pack("<iii", 0, 5, 65536)), id="binary-offsets-past-the-values-buffer"),
    pytest.param(_tampered(_BINARY_OFFSETS, struct.pack("<iii", 0, 8, 5)), id="binary-offsets-that-decrease"),
    pytest.param(_tampered(b"qqzz", b"\xff\xfezz"), id="utf8-values-that-are-not-utf8"),
    pytest.param(_a_dictionary_batch_whose_id_names_no_field(), id="a-dictionary-batch-whose-id-names-no-field"),
    pytest.param(_a_compressed_text_declaring(2**50, "zstd"), id="a-zstd-buffer-declaring-2^50-bytes"),
    pytest.param(_a_compressed_text_declaring(2**50, "lz4"), id="an-lz4-buffer-declaring-2^50-bytes"),
    pytest.param(_an_integer_narrower_than_eight_bits(), id="an-integer-narrower-than-8-bits"),
    pytest.param(_blob_named_on(_table([10, 11], [b"hello", b"world"], ["qq", "zz"]), "id"), id="pylances-blob-type-named-on-a-storage-it-refuses"),
    pytest.param(
        _stream(_table([10, 11], [b"hello", b"world"], ["qq", "zz"])) + _stream(_table([12], [b"three"], ["tt"])), id="a-second-stream-after-the-first"
    ),
    pytest.param(_stream(_table([10, 11], [b"hello", b"world"], ["qq", "zz"])) + b"\x00 bytes after the end", id="bytes-after-the-end-of-the-stream"),
    pytest.param(
        _stream(_table([10, 11], [b"hello", b"world"], ["qq", "zz"]).replace_schema_metadata({b"\xff\xfe": b"x"})), id="schema-metadata-that-is-not-utf8"
    ),
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
    assert "not a valid Arrow IPC stream" in str(refused.json().get("detail")), refused.json()
    assert _rows_on(real_ns_client, branch) == 3, "a refused write must write no rows"


@pytest.mark.parametrize("body", BODIES)
def test_the_insert_branch_arm_refuses_it_without_the_door(tmp_path: Path, body: bytes) -> None:
    """`dataplane.insert_into_table` is a seam of its own: its branch arm must not depend on the door's
    coercion having read the body first, because it is the arm that hands the buffers to Lance."""
    ns = connect("dir", {"root": str(tmp_path / "data")})
    create_table(ns, {}, ["t"], _table([1, 2, 3], [b"a", b"b", b"c"], ["x", "y", "z"]), mode="create")
    open_dataset(ns, {}, ["t"]).create_branch("work", None)

    with pytest.raises(InvalidInputError, match="not a valid Arrow IPC stream"):
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


#: IPC promises 8-byte alignment; decimal128 after an int8 column lands at 8 mod 16 (pyarrow 25.0.0).
_DECIMAL_AFTER_INT8 = pa.table({"a": pa.array([1, 2, 3], pa.int8()), "d": pa.array([Decimal("1.5"), None, Decimal("2.25")], pa.decimal128(10, 2))})


def test_a_valid_body_that_ipc_leaves_unaligned_for_lance_writes_on_every_arm(real_ns_client: TestClient) -> None:
    """Create and the branch arms write the decoded table in-process, where pylance panics on a buffer
    below its type's alignment (pylance 12.0.0); main hands the bytes to the native reader."""
    body = _stream(_DECIMAL_AFTER_INT8)
    assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200

    created = real_ns_client.post("/v1/table/db$d/create", content=body, headers=ARROW_STREAM)
    assert created.status_code == 200, created.text
    assert real_ns_client.post("/v1/table/db$d/branches/create", json={"name": "work"}).status_code == 200
    for door in ("insert", "merge_insert?on=a&when_not_matched_insert_all=true"):
        for query in ("", "branch=work"):
            separator = "&" if "?" in door else "?"
            written = real_ns_client.post(f"/v1/table/db$d/{door}{separator + query if query else ''}", content=body, headers=ARROW_STREAM)
            assert written.status_code == 200, f"{door} {query or 'main'}: {written.status_code} {written.text[:300]}"

    counted = real_ns_client.post("/v1/table/db$d/count_rows", json={"branch": "work"})
    assert (counted.status_code, counted.text) == (200, "6"), counted.text
