"""XC-097: the fleet's one decoder of a caller's Arrow IPC body refuses what framing alone lets through.

Most tampered bodies below parse as IPC and lie in their buffers, names or metadata; the reader
refuses the rest outright. Read without full validation, the first kinds read process memory or
abort the process, a name that is not UTF-8 raises from whatever touches it next, metadata
that is not UTF-8 fails a later Lance write (pylance 12.0.0), and the reader itself raises a
`KeyError`, `MemoryError` or `NotImplementedError` for the last kinds (pyarrow 25.0.0). Reading a
body also runs every registered extension type's deserializer on the caller's metadata, which raises
whatever that library raises. All of them are refused here as `ArrowBodyError`, before any caller
sees a row. The two-batch bodies pin that every batch is read, not only the first.
"""

from __future__ import annotations

import importlib
import struct
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from service_kit.lancekit.arrow_ipc import ArrowBodyError, decode_arrow_stream, decode_arrow_stream_or_file, encode_arrow_stream


#: A caller's body cap, far above every body here: these bodies are refused for what they hold, not their size.
_LIMIT = 64 * 1024 * 1024
_OFFSETS = struct.pack("<iii", 0, 5, 10)
_NOT_UTF8 = b"\xff\xfe\xfd\xfc"


def _file(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _rewritten(body: bytes, good: bytes, bad: bytes) -> bytes:
    assert body.count(good) == 1, "the bytes to tamper with are not unique in the body"
    return body.replace(good, bad)


def _two_values(kind: pa.DataType) -> pa.Table:
    values: list[Any] = [b"hello", b"world"] if kind == pa.binary() else ["hello", "world"]
    return pa.table({"v": pa.array(values, kind)})


class _Labelled(pa.ExtensionType):
    """A registered extension type that keeps the storage type it is read with, as a parametric one does.

    So the decoder meets a real `BaseExtensionType` whose nested names come from the body.
    """

    def __init__(self, storage_type: pa.DataType) -> None:
        super().__init__(storage_type, "rask.test.labelled")

    def __arrow_ext_serialize__(self) -> bytes:
        return b""

    @classmethod
    def __arrow_ext_deserialize__(cls, storage_type: pa.DataType, serialized: bytes) -> _Labelled:
        return cls(storage_type)


_LABELLED_STORAGE = pa.struct([("zzzq", pa.string())])


@pytest.fixture
def _labelled_registered() -> Any:
    pa.register_extension_type(_Labelled(_LABELLED_STORAGE))
    yield
    pa.unregister_extension_type("rask.test.labelled")


def _stream(table: pa.Table) -> bytes:
    return encode_arrow_stream(table)


def _field_metadata_rewritten(field: pa.Field, values: list[Any], good: bytes) -> bytes:
    """A one-column body whose `field` carries metadata, with the metadata bytes `good` made not UTF-8."""
    return _rewritten(_stream(pa.table({field.name: values}, schema=pa.schema([field]))), good, _NOT_UTF8)


#: Two batches whose offsets differ, so the second batch's can be tampered with alone.
_TWO_BATCHES = pa.Table.from_batches([pa.record_batch({"v": ["hello", "world"]}), pa.record_batch({"v": ["abc", "defghij"]})])


#: IPC promises 8-byte alignment; decimal128 after an int8 column lands at 8 mod 16 (pyarrow 25.0.0).
_DECIMAL_AFTER_INT8 = pa.table({"a": pa.array([1, 2, 3], pa.int8()), "d": pa.array([Decimal("1.5"), None, Decimal("2.25")], pa.decimal128(10, 2))})


TAMPERED = [
    pytest.param(b"this is not an arrow ipc stream", id="a-body-that-is-not-arrow"),
    pytest.param(_rewritten(_stream(_two_values(pa.binary())), _OFFSETS, struct.pack("<iii", 0, 8, 5)), id="binary-offsets-that-decrease"),
    pytest.param(_rewritten(_stream(_two_values(pa.string())), b"hello", b"\xff\xfello"), id="utf8-values-that-are-not-utf8"),
    pytest.param(
        _rewritten(_stream(pa.table({"d": pa.DictionaryArray.from_arrays(pa.array([0], pa.int32()), pa.array([{"zzzq": 1}]))})), b"zzzq", _NOT_UTF8),
        id="a-dictionary-value-field-name-that-is-not-utf8",
    ),
    pytest.param(
        _rewritten(_stream(pa.table({"a": [1]}).replace_schema_metadata({"k": "vvvq"})), b"vvvq", _NOT_UTF8), id="a-schema-metadata-value-that-is-not-utf8"
    ),
    pytest.param(_stream(_TWO_BATCHES)[:-20], id="a-stream-cut-inside-its-second-batch"),
    pytest.param(_stream(_TWO_BATCHES) + b"\x00 bytes after the end of the stream", id="bytes-after-the-end-of-the-stream"),
]


@pytest.mark.parametrize("body", TAMPERED)
def test_a_tampered_stream_is_refused(body: bytes) -> None:
    with pytest.raises(ArrowBodyError):
        decode_arrow_stream(body, max_bytes=_LIMIT)


def test_a_tampered_file_is_refused() -> None:
    body = _rewritten(_file(_two_values(pa.binary())), _OFFSETS, struct.pack("<iii", 0, 5, 65536))

    with pytest.raises(ArrowBodyError):
        decode_arrow_stream_or_file(body, max_bytes=_LIMIT)


def test_a_field_name_under_a_registered_extension_type_is_decoded(_labelled_registered: Any) -> None:
    storage = pa.array([{"zzzq": "x"}], _LABELLED_STORAGE)
    table = pa.table({"e": pa.ExtensionArray.from_storage(_Labelled(_LABELLED_STORAGE), storage)})
    body = _rewritten(_stream(table), b"zzzq", _NOT_UTF8)

    with pytest.raises(ArrowBodyError):
        decode_arrow_stream(body, max_bytes=_LIMIT)


#: What a registered library's deserializer raises for extension metadata it refuses, by the metadata
#: key the type below writes; `json.loads` on bad metadata is the ValueError.
_DESERIALIZER_RAISES: dict[bytes, type[Exception]] = {b"value": ValueError, b"key": KeyError, b"type": TypeError, b"runtime": RuntimeError}


class _RefusesItsMetadata(pa.ExtensionType):
    """A registered type whose deserializer raises the class its metadata names, as a library's does on metadata it refuses."""

    def __init__(self, raises: bytes) -> None:
        self.raises = raises
        super().__init__(pa.int64(), "rask.test.refuses")

    def __arrow_ext_serialize__(self) -> bytes:
        return self.raises

    @classmethod
    def __arrow_ext_deserialize__(cls, storage_type: pa.DataType, serialized: bytes) -> _RefusesItsMetadata:
        raise _DESERIALIZER_RAISES[serialized]("the metadata is not this type's")


@pytest.fixture
def _refusing_type_registered() -> Any:
    pa.register_extension_type(_RefusesItsMetadata(b"value"))
    yield
    pa.unregister_extension_type("rask.test.refuses")


@pytest.mark.parametrize("raises", [b"key", b"runtime"])
@pytest.mark.parametrize("decode", [decode_arrow_stream], ids=["stream"])
def test_what_a_registered_deserializer_raises_is_a_refusal(_refusing_type_registered: Any, raises: bytes, decode: Callable[..., pa.Table]) -> None:
    """Reading a body runs every registered type's deserializer on metadata the caller chose, and each raises its own classes."""
    body = _stream(pa.table({"e": pa.ExtensionArray.from_storage(_RefusesItsMetadata(raises), pa.array([1]))}))

    with pytest.raises(ArrowBodyError, match="the metadata is not this type's"):
        decode(body, max_bytes=_LIMIT)


@pytest.mark.parametrize("decode", [decode_arrow_stream], ids=["stream"])
def test_pylances_blob_type_named_on_a_storage_it_refuses_is_a_refusal(decode: Callable[..., pa.Table]) -> None:
    """`import lance` registers `lance.blob.v2`, and every service that decodes a caller's body imports lance.

    Its deserializer raises TypeError for a storage type that is not its struct (pylance 12.0.0).
    """
    importlib.import_module("lance")
    blob_named = pa.field("v", pa.int64(), metadata={b"ARROW:extension:name": b"lance.blob.v2", b"ARROW:extension:metadata": b""})
    body = _stream(pa.table([pa.array([1])], schema=pa.schema([blob_named])))

    with pytest.raises(ArrowBodyError, match="BlobType storage type must be a struct"):
        decode(body, max_bytes=_LIMIT)


@pytest.mark.parametrize(
    ("body", "names"),
    [
        pytest.param(
            _rewritten(_stream(pa.table({"s": [{"zzzq": 1}]})), b"zzzq", _NOT_UTF8),
            "the name of child 0 of column 0 ('s') is not UTF-8",
            id="a-nested-name",
        ),
        pytest.param(
            _field_metadata_rewritten(pa.field("a", pa.int64(), metadata={"k": "vvvq"}), [1], b"vvvq"),
            "the metadata of column 0 ('a') holds a key or value that is not UTF-8",
            id="field-metadata",
        ),
        pytest.param(
            _rewritten(_stream(pa.table({"a": [1]}).replace_schema_metadata({"kkkq": "v"})), b"kkkq", _NOT_UTF8),
            "the schema's metadata holds a key or value that is not UTF-8",
            id="schema-metadata",
        ),
    ],
)
def test_a_refused_name_or_metadata_is_named_by_where_it_sits(body: bytes, names: str) -> None:
    with pytest.raises(ArrowBodyError) as refused:
        decode_arrow_stream(body, max_bytes=_LIMIT)

    assert str(refused.value) == names
    assert isinstance(refused.value.__cause__, UnicodeDecodeError), "the refusal's cause is the decode that failed"


@pytest.mark.parametrize("framing", ["stream", "file"])
def test_a_decoded_table_is_aligned_as_lance_reads_it(framing: str, tmp_path: Path) -> None:
    """pylance (arrow-rs) takes pyarrow's buffers in place and needs each at its type's own alignment.

    Handed decimal128 at 8 mod 16 it panics with a `PanicException`, which derives from BaseException and
    so escapes every `except Exception` around the write (pylance 12.0.0).
    """
    lance = importlib.import_module("lance")
    body = _stream(_DECIMAL_AFTER_INT8) if framing == "stream" else _file(_DECIMAL_AFTER_INT8)

    lance.write_dataset(decode_arrow_stream_or_file(body, max_bytes=_LIMIT), str(tmp_path / "t"))

    assert lance.dataset(str(tmp_path / "t")).to_table().equals(_DECIMAL_AFTER_INT8)


def test_a_stream_refused_for_its_buffers_is_refused_for_its_buffers() -> None:
    """The framing is read from the file magic, so a stream is never re-read as a file and refused for
    a reason that has nothing to do with what is wrong with it."""
    body = _rewritten(_stream(_two_values(pa.binary())), _OFFSETS, struct.pack("<iii", 0, 8, 5))

    with pytest.raises(ArrowBodyError, match="non-monotonic offset"):
        decode_arrow_stream_or_file(body, max_bytes=_LIMIT)


def test_the_stream_decoder_takes_no_file() -> None:
    """A stream-only door (the catalog's, whose native reader takes no file) must not accept one here."""
    with pytest.raises(ArrowBodyError):
        decode_arrow_stream(_file(_two_values(pa.string())), max_bytes=_LIMIT)


@pytest.mark.parametrize("framing", ["stream", "file"])
def test_a_valid_body_decodes_to_the_table_it_carries(framing: str) -> None:
    table = pa.table({"v": ["hello", "world"], "n": [1, 2], "s": [{"a": 1}, {"a": 2}]})
    body = _stream(table) if framing == "stream" else _file(table)

    assert decode_arrow_stream_or_file(body, max_bytes=_LIMIT).equals(table)


@pytest.mark.parametrize(("spelling", "decode"), [(str, decode_arrow_stream), (Path, decode_arrow_stream_or_file)])
def test_a_path_is_refused_rather_than_opened(decode: Callable[..., pa.Table], spelling: Callable[[Path], Any], tmp_path: Path) -> None:
    """pyarrow reads a str or a path-like from disk, so a decoder of a caller's body takes bytes only."""
    on_disk = tmp_path / "body.arrows"
    on_disk.write_bytes(_stream(pa.table({"v": ["on-disk"]})))

    with pytest.raises(TypeError, match=f"got {type(spelling(on_disk)).__name__}"):
        decode(spelling(on_disk), max_bytes=_LIMIT)
