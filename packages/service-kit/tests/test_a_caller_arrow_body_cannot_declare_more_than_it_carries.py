"""XC-104: a caller's Arrow body cannot declare more than its bytes carry, nor inflate past the door's body cap.

A type that stores data per value costs at least one bit for each, so an honest body carries at most
eight values per inflated byte at each field. A type that stores nothing per value (null, an empty
struct, `fixed_size_binary(0)`, a batch with no columns) lets the metadata declare a count no byte
backs: 224 bytes of `pa.nulls` declare a million rows, and a `to_pylist` of 30 million grew the process
by 5,978 MB (pyarrow 25.0.0). The bound reads only counts, never a field's type, so one zero-cost type
stands for all of them below. A file's footer can name one batch any number of times. A
compressed buffer is inflated as it is read, past the body cap that counted the bytes sent: a 51 KB zstd
body grew the reader by 1,603 MB. Compression itself stays accepted, because lancedb's remote client
LZ4-compresses every create and insert and Lance's own namespace backend reads it.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from typing import cast

import pyarrow as pa
import pytest

from service_kit.lancekit.arrow_ipc import ArrowBodyError, ArrowBodyTooLargeError, decode_arrow_stream, decode_arrow_stream_or_file


#: A cap far above every body below unless a test says otherwise.
_LIMIT = 64 * 1024 * 1024
#: Every pool a test measures on, held for the process: a buffer allocated from one can outlive the
#: test (an app or Lance cache keeps it), and freed after its pool it dereferences freed memory, which
#: measured as a segfault of the test process.
_POOLS: list[pa.MemoryPool] = []


def _a_pool_of_its_own() -> pa.MemoryPool:
    pool = pa.proxy_memory_pool(pa.default_memory_pool())
    _POOLS.append(pool)
    return pool


_END_OF_STREAM = b"\xff\xff\xff\xff\x00\x00\x00\x00"


def _stream(table: pa.Table, compression: str | None = None) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema, options=pa.ipc.IpcWriteOptions(compression=compression)) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _file(table: pa.Table, compression: str | None = None) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, table.schema, options=pa.ipc.IpcWriteOptions(compression=compression)) as writer:
        for batch in table.to_batches():
            writer.write_batch(batch)
    return sink.getvalue().to_pybytes()


def _messages(body: bytes) -> list[bytes]:
    return [message.serialize().to_pybytes() for message in pa.ipc.MessageReader.open_stream(body)]


def _one_column(array: pa.Array) -> bytes:
    return _stream(pa.table({"c": array}))


def _no_columns(rows: int) -> pa.Table:
    return pa.Table.from_batches([pa.RecordBatch.from_struct_array(pa.StructArray.from_buffers(pa.struct([]), rows, [None]))])


def _rewritten(body: bytes, old: int, new: int) -> bytes:
    assert body.count(struct.pack("<q", old)) == 1, f"{old} is not unique in the body"
    return body.replace(struct.pack("<q", old), struct.pack("<q", new))


def _a_footer_naming_one_batch_again(times: int, rows: int) -> bytes:
    """A file of one `rows`-row int8 batch and `times` one-row batches, whose footer names the big one `times` more times."""
    batches = [pa.record_batch({"v": pa.array([1] * rows, pa.int8())})] + [pa.record_batch({"v": pa.array([2], pa.int8())})] * times
    body = bytearray(_file(pa.Table.from_batches(batches)))
    footer_end = len(body) - 10
    footer_start = footer_end - struct.unpack_from("<i", body, footer_end)[0]
    found = []
    for at in range(footer_start, footer_end - 23):
        offset, metadata, length = struct.unpack_from("<qi4xq", body, at)
        if (
            0 < offset < footer_start
            and body[offset : offset + 4] == b"\xff\xff\xff\xff"
            and 0 < metadata < 4096
            and offset + metadata + length <= footer_start
        ):
            found.append((at, offset, metadata, length))
    record_blocks = found[-(times + 1) :]
    assert len(record_blocks) == times + 1, "the footer's record-batch blocks were not all found"
    _, *big = max(record_blocks, key=lambda block: block[3])
    for at, *_ in record_blocks:
        struct.pack_into("<qi4xq", body, at, *big)
    return bytes(body)


def _only_the_dictionary_compressed(table: pa.Table) -> bytes:
    """An uncompressed record batch whose dictionary batch arrives zstd-compressed."""
    plain, packed = _messages(_stream(table)), _messages(_stream(table, "zstd"))
    return b"".join([plain[0], packed[1], plain[2], _END_OF_STREAM])


DECLARING_MORE = [
    pytest.param(_one_column(pa.nulls(10**6)), id="a-null-column"),
    pytest.param(_stream(_no_columns(10**6)), id="a-batch-with-no-columns"),
    pytest.param(_one_column(pa.LargeListArray.from_arrays(pa.array([0, 10**6], pa.int64()), pa.nulls(10**6))), id="one-row-listing-a-million-nulls"),
    pytest.param(_one_column(pa.DictionaryArray.from_arrays(pa.array([0], pa.int8()), pa.nulls(10**6))), id="a-dictionary-of-a-million-nulls"),
    pytest.param(_stream(pa.Table.from_batches([pa.record_batch({"c": pa.nulls(10**4)})] * 1000)), id="many-batches-each-within-the-bound-alone"),
]


@pytest.mark.parametrize("body", DECLARING_MORE)
def test_a_body_declaring_more_values_than_its_bytes_carry_is_refused(body: bytes) -> None:
    with pytest.raises(ArrowBodyError, match="can carry at most"):
        decode_arrow_stream(body, max_bytes=_LIMIT)


def test_a_file_whose_footer_names_one_batch_again_is_refused() -> None:
    """The file reader decodes what the footer names, so a block named 101 times is 101 batches from one's bytes."""
    with pytest.raises(ArrowBodyError, match="can carry at most"):
        decode_arrow_stream_or_file(_a_footer_naming_one_batch_again(100, 10**5), max_bytes=_LIMIT)


def test_a_batch_declaring_a_negative_length_is_refused() -> None:
    """Read as zero rows, a negative length would cancel another batch's count in the sum the bound is taken over."""
    with pytest.raises(ArrowBodyError, match="negative"):
        decode_arrow_stream(_rewritten(_stream(_no_columns(12345)), 12345, -1), max_bytes=_LIMIT)


_TEXT = pa.table({"s": ["x" * 100_000] * 10})
_LIMIT_BELOW_INFLATED = _TEXT.nbytes // 2


@pytest.mark.parametrize(
    ("body", "decode"),
    [
        pytest.param(_stream(_TEXT, "lz4"), decode_arrow_stream, id="an-lz4-stream"),
        pytest.param(_file(_TEXT, "zstd"), decode_arrow_stream_or_file, id="a-zstd-file"),
        pytest.param(
            _only_the_dictionary_compressed(pa.table({"d": pa.array(["x" * 100_000 + str(i) for i in range(10)]).dictionary_encode()})),
            decode_arrow_stream,
            id="a-compressed-dictionary-batch",
        ),
        pytest.param(
            _file(pa.table({"d": pa.array(["x" * 100_000 + str(i) for i in range(10)]).dictionary_encode()}), "zstd"),
            decode_arrow_stream_or_file,
            id="a-compressed-dictionary-in-a-file",
        ),
    ],
)
def test_a_body_inflating_past_the_limit_is_refused_before_it_is_read(body: bytes, decode: Callable[..., pa.Table]) -> None:
    assert len(body) < _LIMIT_BELOW_INFLATED, "the body is over the limit as sent, so this would not test inflation"

    with pytest.raises(ArrowBodyTooLargeError, match="inflates to"):
        decode(body, max_bytes=_LIMIT_BELOW_INFLATED)


def test_an_uncompressed_body_over_the_limit_is_refused() -> None:
    """The door's own middleware counts the bytes sent too; the decoder holds the limit wherever it is called."""
    with pytest.raises(ArrowBodyTooLargeError, match=r"over the .*-byte limit"):
        decode_arrow_stream(_stream(_TEXT), max_bytes=_LIMIT_BELOW_INFLATED)


def test_a_compressed_buffer_declaring_a_negative_length_is_refused() -> None:
    """Summed with its neighbours, a negative inflated length would cancel another buffer's growth under the limit."""
    body = _stream(pa.table({"s": ["x" * 1003]}), "zstd")
    prefix = body.rindex(struct.pack("<q", 1003))  # the values buffer's; the offsets buffer ends in 1003 too

    with pytest.raises(ArrowBodyError, match="negative"):
        decode_arrow_stream(body[:prefix] + struct.pack("<q", -2) + body[prefix + 8 :], max_bytes=_LIMIT)


def test_a_buffer_left_uncompressed_in_a_compressed_body_is_read() -> None:
    """Arrow's `-1` prefix marks a buffer stored as is; apache-arrow JS writes it for every buffer compression did not shrink."""
    table = pa.table({"v": pa.array([12345678], pa.int64())})
    raw = struct.pack("<q", 12345678)
    frame = pa.Codec("zstd").compress(raw).to_pybytes()
    body = _stream(table, "zstd")
    compressed = struct.pack("<q", len(raw)) + frame
    assert body.count(compressed) == 1 and len(frame) > len(raw), "the values buffer is not where this test rewrites it"
    as_is = struct.pack("<q", -1) + raw + b"\0" * (len(frame) - len(raw))

    assert decode_arrow_stream(body.replace(compressed, as_is), max_bytes=_LIMIT).equals(table)


_BOOLEANS = pa.array([True, False] * 50_000)


@pytest.mark.parametrize(
    "table",
    [
        pytest.param(pa.table({"b": _BOOLEANS}), id="booleans"),
        pytest.param(pa.table({"s": pa.StructArray.from_arrays([_BOOLEANS], ["b"])}), id="a-struct-of-booleans"),
        pytest.param(pa.table({"n": pa.nulls(len(_BOOLEANS)), "b": _BOOLEANS}), id="a-null-column-beside-booleans"),
    ],
)
@pytest.mark.parametrize("compression", [None, "lz4"], ids=["uncompressed", "lz4"])
@pytest.mark.parametrize("framing", [_stream, _file], ids=["stream", "file"])
def test_an_honest_body_at_its_densest_is_decoded(table: pa.Table, compression: str | None, framing: Callable[[pa.Table, str | None], bytes]) -> None:
    """A boolean is one bit per value, the densest a stored value gets; compressed, it is judged by what it inflates to."""
    assert decode_arrow_stream_or_file(framing(table, compression), max_bytes=_LIMIT).equals(table)


@pytest.mark.parametrize("framing", [_stream, _file], ids=["stream", "file"])
def test_a_body_refused_for_its_size_is_refused_before_anything_inflates(framing: Callable[[pa.Table, str | None], bytes]) -> None:
    """The bound is a MEMORY bound only if it runs before the read: refused after inflating, the refusal
    would cost what the body inflates to. Measured on a pool of its own, so no earlier allocation hides it."""
    body = framing(pa.table({"s": ["x" * (16 * 1024 * 1024)] * 3}), "lz4")
    pool, previous = _a_pool_of_its_own(), pa.default_memory_pool()
    pa.set_memory_pool(pool)
    try:
        with pytest.raises(ArrowBodyTooLargeError):
            decode_arrow_stream_or_file(body, max_bytes=1024 * 1024)
    finally:
        pa.set_memory_pool(previous)

    assert pool.max_memory() < 1024 * 1024, f"the refusal allocated {pool.max_memory():,} bytes of a 48 MiB inflation first"


@pytest.mark.parametrize(
    ("cap", "raised"),
    [pytest.param(None, TypeError, id="none"), pytest.param(True, TypeError, id="a-bool"), pytest.param(0, ValueError, id="zero")],
)
def test_a_misconfigured_cap_is_the_doors_error_not_the_bodys(cap: object, raised: type[Exception]) -> None:
    """A cap that is not a positive byte count is a wiring fault, and must not answer 400 blaming the body."""
    with pytest.raises(raised):
        decode_arrow_stream(_stream(pa.table({"v": [1]})), max_bytes=cast("int", cap))
