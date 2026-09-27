"""The ONE Arrow-IPC **stream** encoder, the ONE validating decoder of a caller's Arrow body, and the media type.

The encoder writes the wire the Lance catalog's write doors (``/v1/table/{id}/create``, ``/insert``,
``/merge_insert``) parse and the annotation engine consumes zero-copy. It is a **stream**, never an
IPC *file*: a file body fails the catalog's Rust reader with "failed to fill whole buffer". The read
side returns IPC files — that asymmetry is the catalog's contract, not this encoder's choice.

The decoders exist because IPC framing that parses says nothing about the buffers it frames. An offset
past its values buffer reads process memory, an offset that decreases aborts the process, and a
string value or field name that is not UTF-8 raises from whatever touches it next (pyarrow 25.0.0).
``Table.validate(full=True)`` refuses the buffers — plain ``validate()`` accepts decreasing offsets and
non-UTF-8 values — and reads top-level column names; nested names and all metadata are decoded here,
because nothing in pyarrow checks them.

A caller's body is also held to what its bytes can carry, before any batch is read
(:func:`_refuse_what_its_bytes_cannot_carry`): a field of a type that stores nothing per value
declares any count it likes, and a compressed buffer is inflated as it is read, so the door passes
the limit its own body cap sets. A response from a rask service is not held to that, because an
honest projection of such a field is dense too.
Every service that reads Arrow bytes reads them through this module — the catalog's write doors
through :func:`decode_arrow_stream`, the annotator's import through
:func:`decode_arrow_stream_or_file`, the catalog's ``/query`` response through
:func:`decode_arrow_response`; pinned by
``tests/unit/test_a_caller_arrow_body_is_decoded_by_the_validating_decoder.py``.

``pyarrow`` is imported inside the functions, not at module scope, so a module that only needs
:data:`ARROW_STREAM_MEDIA_TYPE` (a header value on a publish path that keeps the heavy import off its
own import time) can take the constant without pulling pyarrow.
"""

from __future__ import annotations

import struct
from collections import Counter
from contextlib import contextmanager
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    import pyarrow as pa


#: The Arrow-IPC **stream** media type — the ``Content-Type`` every catalog write body carries and the
#: type the ``/points`` / annotations read responses are served as. One spelling, one import.
ARROW_STREAM_MEDIA_TYPE = "application/vnd.apache.arrow.stream"

#: The IPC file format's leading magic. A stream opens with a message length instead, so the framing
#: of a body is read from its first six bytes rather than guessed by trying one reader after another.
_FILE_MAGIC = b"ARROW1"

#: The fewest bits a value of a type that stores data per value costs: a boolean's one. An honest body
#: therefore carries at most eight values per inflated byte at any field; a type that stores nothing per
#: value (null, an empty struct, ``fixed_size_binary(0)``, a batch with no columns) or per run
#: (run-end-encoded) declares more. Measured on pyarrow 25.0.0: 224 bytes of ``pa.nulls`` declare a
#: million rows, and a ``to_pylist`` of 30 million such rows grew the process by 5,978 MB.
_VALUES_PER_BYTE = 8

#: The ``MessageHeader`` members read below (Arrow format/Message.fbs, apache-arrow-25.0.0).
_DICTIONARY_BATCH = 2
_RECORD_BATCH = 3

#: A compressed buffer's leading int64 when its bytes were left uncompressed (Message.fbs, BodyCompression).
_LEFT_UNCOMPRESSED = -1

#: A ``Block`` in the file footer: offset (int64), metaDataLength (int32, padded to 8), bodyLength (int64).
_BLOCK_WIDTH = 24
#: A ``FieldNode`` in a ``RecordBatch``: length (int64), null_count (int64).
_FIELD_NODE_WIDTH = 16
#: A ``Buffer`` in a ``RecordBatch``: offset into the message body (int64), length (int64).
_BUFFER_WIDTH = 16


class ArrowBodyError(ValueError):
    """Bytes that are not a valid Arrow IPC body — in their framing, buffers, names, metadata, or an
    extension type's own metadata.

    A ``ValueError`` rather than a response type: each door maps it to its own refusal (the catalog to
    ``InvalidInputError``, the annotator's import to ``ValidationError``). The message is the reader's,
    which names what is at fault and never the memory it refused to read; the reader's exception is
    its ``__cause__``.
    """


class ArrowBodyTooLargeError(ArrowBodyError):
    """An Arrow body over the door's body cap once its compressed buffers inflate: well formed, too large.

    Its own class so a door can say "too large" rather than "not valid Arrow" to an honest client whose
    compressed body inflates past the cap.
    """


def encode_arrow_stream(table: pa.Table) -> bytes:
    """Serialize ``table`` as an Arrow-IPC stream. Pass ``schema.empty_table()`` to encode an
    empty, correctly-typed body (the shape a ``create`` sends)."""
    import pyarrow as pa

    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def decode_arrow_stream(data: bytes, *, max_bytes: int) -> pa.Table:
    """A caller's Arrow IPC **stream** as a table validated in full, or :class:`ArrowBodyError`.

    Held to what its bytes can carry, and to ``max_bytes`` once inflated, before any batch is read.
    Read whole, because a stream cut inside a batch parses its schema and fails only on the batch, and
    refused unless the stream is the whole body: a reader stops at the first end-of-stream marker, so
    a second stream or trailing bytes would be dropped while the door answers for the rest.

    Args:
        data: the request body.
        max_bytes: the door's own body cap, which the body may not exceed once its buffers inflate.

    Raises:
        TypeError: ``data`` is not bytes, or ``max_bytes`` is not an int.
        ValueError: ``max_bytes`` is below 1.
        ArrowBodyTooLargeError: the body is over ``max_bytes`` once inflated.
        ArrowBodyError: the body is not a valid Arrow IPC stream, or declares more than its bytes carry.
    """
    _require_bytes(data)
    _require_cap(max_bytes)
    with _refused_as_arrow_body_error():
        _refuse_what_its_bytes_cannot_carry(data, _stream_messages(data), max_bytes=max_bytes)
    return _decoded_stream(data)


def decode_arrow_stream_or_file(data: bytes, *, max_bytes: int) -> pa.Table:
    """:func:`decode_arrow_stream`, for a door that also takes the IPC **file** framing.

    Both are what pyarrow writes depending on which writer a caller reached for; the file magic tells
    them apart, so a stream whose buffers fail validation is refused for its buffers, not re-read as a
    file and refused for the wrong reason.

    Raises:
        TypeError: ``data`` is not bytes, or ``max_bytes`` is not an int.
        ValueError: ``max_bytes`` is below 1.
        ArrowBodyTooLargeError: the body is over ``max_bytes`` once inflated.
        ArrowBodyError: the body is not a valid Arrow IPC stream or file, or declares more than its
            bytes carry.
    """
    _require_bytes(data)
    _require_cap(max_bytes)
    if not data.startswith(_FILE_MAGIC):
        return decode_arrow_stream(data, max_bytes=max_bytes)
    with _refused_as_arrow_body_error():
        _refuse_what_its_bytes_cannot_carry(data, _file_messages(data), max_bytes=max_bytes)
    return _decoded_file(data)


def decode_arrow_response(data: bytes) -> pa.Table:
    """A rask service's Arrow IPC response, stream or file, validated in full but not held to its size.

    Not held to :func:`_refuse_what_its_bytes_cannot_carry`, because what bounds a response is the query
    that asked for it, and an honest one is denser than a caller's body may be: measured on pylance
    12.0.0, the ``dir`` backend's ``query_table`` answers a projection of a null column as one 490-byte
    batch whatever its row count, 2,041 rows per byte at a million rows.

    Raises:
        TypeError: ``data`` is not bytes.
        ArrowBodyError: the bytes are not a valid Arrow IPC stream or file.
    """
    _require_bytes(data)
    return _decoded_file(data) if data.startswith(_FILE_MAGIC) else _decoded_stream(data)


def _decoded_stream(data: bytes) -> pa.Table:
    import pyarrow as pa

    with _refused_as_arrow_body_error():
        source = pa.BufferReader(data)
        table = _validated(pa.ipc.open_stream(source, options=_aligned_as_lance_reads_it()).read_all())
        if (unread := len(data) - source.tell()) > 0:
            raise ArrowBodyError(f"{unread} bytes follow the end of the stream")
        return table


def _decoded_file(data: bytes) -> pa.Table:
    import pyarrow as pa

    with _refused_as_arrow_body_error():
        return _validated(pa.ipc.open_file(data, options=_aligned_as_lance_reads_it()).read_all())


def _refuse_what_its_bytes_cannot_carry(data: bytes, messages: Iterable[tuple[bytes, pa.Buffer | None]], *, max_bytes: int) -> None:
    """Refuse a body over ``max_bytes`` once inflated, a negative count, or more values at one field than it carries.

    Read from each batch's metadata and each compressed buffer's length prefix before any batch is
    read, because reading it is what costs: a compressed buffer is inflated as it is read, past a body
    cap that counted the bytes sent (measured on pyarrow 25.0.0: a 51 KB zstd body grew the reader by
    1,603 MB). Compression itself is Arrow's and stays accepted: lancedb's remote client LZ4-compresses
    every create and insert with no switch to turn it off, and Lance's own namespace backend reads it.
    The count bound is taken over the inflated size and summed per field over every batch the reader
    decodes, so neither many small batches nor a file footer naming one batch many times escapes it,
    and a negative count or length is refused because it would cancel another's.
    """
    inflated = len(data)
    if inflated > max_bytes:
        raise ArrowBodyTooLargeError(f"the body is {inflated:,} bytes, over the {max_bytes:,}-byte limit on a body")
    declared: Counter[tuple[int | None, int]] = Counter()
    for metadata, body in messages:
        carried = _batch_in(metadata)
        if carried is None:
            continue
        dictionary, batch = carried
        if _offset(metadata, batch, 3) is not None:
            inflated += _growth_on_inflating(metadata, batch, memoryview(body if body is not None else b""))
            if inflated > max_bytes:
                raise ArrowBodyTooLargeError(f"the body inflates to at least {inflated:,} bytes, over the {max_bytes:,}-byte limit on a body")
        ceiling = _VALUES_PER_BYTE * inflated
        rows = _scalar(metadata, batch, 0, "<q")
        nodes = [_read(metadata, "<q", at) for at in _vector(metadata, batch, 1, _FIELD_NODE_WIDTH)]
        for field, count in ((-1, rows), *enumerate(nodes)):
            if count < 0:
                raise ArrowBodyError(f"a batch declares a negative count ({count})")
            declared[dictionary, field] += count
            if declared[dictionary, field] > ceiling:
                raise ArrowBodyError(
                    f"the body declares {declared[dictionary, field]:,} values at one field, and its {inflated:,} bytes can carry at most"
                    f" {ceiling:,} values: a type that stores data per value costs at least one bit for each"
                )


def _growth_on_inflating(metadata: bytes, batch: int, body: memoryview) -> int:
    """How many bytes a compressed batch's buffers gain as they are inflated, read from their length prefixes."""
    growth = 0
    for at in _vector(metadata, batch, 2, _BUFFER_WIDTH):
        offset, stored = _read(metadata, "<q", at), _read(metadata, "<q", at + 8)
        if stored == 0:
            continue
        if stored < 8:
            raise ArrowBodyError(f"a compressed buffer of {stored} bytes has no room for its length prefix")
        declared = _read(body, "<q", offset)
        inflated = stored - 8 if declared == _LEFT_UNCOMPRESSED else declared
        if inflated < 0:
            raise ArrowBodyError(f"a compressed buffer declares a negative length ({declared})")
        growth += inflated - stored
    return growth


def _stream_messages(data: bytes) -> Iterator[tuple[bytes, pa.Buffer | None]]:
    """Each message's metadata and raw body, in the order the stream reader decodes them; no body is inflated."""
    import pyarrow as pa

    for message in pa.ipc.MessageReader.open_stream(pa.BufferReader(data)):
        yield message.metadata.to_pybytes(), message.body


def _file_messages(data: bytes) -> Iterator[tuple[bytes, pa.Buffer | None]]:
    """The metadata and raw body of each dictionary and record batch the file's footer names, in the reader's order.

    Walked from the footer rather than through the file, because the reader decodes what the footer
    names, and a footer can name one batch any number of times. Opening the file first has pyarrow
    verify the footer's flatbuffer, and the batch count it reads must match the one walked here.
    """
    import pyarrow as pa

    batches = pa.ipc.open_file(data).num_record_batches
    end = len(data) - len(_FILE_MAGIC) - 4
    size = _read(data, "<i", end)
    if not 0 < size <= end:
        raise ArrowBodyError("the file's footer lies outside the file")
    footer = data[end - size : end]
    root = _read(footer, "<I", 0)
    dictionaries, records = (list(_vector(footer, root, slot, _BLOCK_WIDTH)) for slot in (2, 3))
    if len(records) != batches:
        raise ArrowBodyError(f"the file's footer names {len(records)} batches here and {batches} to the reader")
    whole = pa.py_buffer(data)
    for at in (*dictionaries, *records):
        offset, length = _read(footer, "<q", at), _read(footer, "<i", at + 8) + _read(footer, "<q", at + 16)
        if length < 0 or not 0 <= offset <= len(data) - length:
            raise ArrowBodyError("a block the file's footer names lies outside the file")
        message = pa.ipc.read_message(whole.slice(offset, length))
        yield message.metadata.to_pybytes(), message.body


def _batch_in(metadata: bytes) -> tuple[int | None, int] | None:
    """The record batch a message carries, as its dictionary id (None for a record batch) and the table's position."""
    message = _read(metadata, "<I", 0)
    kind, header = _scalar(metadata, message, 1, "<B"), _offset(metadata, message, 2)
    if header is None:
        return None
    if kind == _RECORD_BATCH:
        return None, header
    if kind == _DICTIONARY_BATCH and (data := _offset(metadata, header, 1)) is not None:
        return _scalar(metadata, header, 0, "<q"), data
    return None


def _read(buffer: bytes | memoryview, fmt: str, at: int) -> int:
    width = struct.calcsize(fmt)
    if not 0 <= at <= len(buffer) - width:
        raise ArrowBodyError("the body's IPC metadata points outside itself")
    return int(struct.unpack_from(fmt, buffer, at)[0])


def _field_at(buffer: bytes, table: int, slot: int) -> int | None:
    """Where field ``slot`` of the flatbuffer table at ``table`` is stored, or None when its vtable omits it."""
    vtable = table - _read(buffer, "<i", table)
    entry = 4 + 2 * slot
    if entry + 2 > _read(buffer, "<H", vtable):
        return None
    relative = _read(buffer, "<H", vtable + entry)
    return table + relative if relative else None


def _scalar(buffer: bytes, table: int, slot: int, fmt: str) -> int:
    """A scalar field; every one read here defaults to zero when absent (Message.fbs)."""
    at = _field_at(buffer, table, slot)
    return 0 if at is None else _read(buffer, fmt, at)


def _offset(buffer: bytes, table: int, slot: int) -> int | None:
    at = _field_at(buffer, table, slot)
    return None if at is None else at + _read(buffer, "<I", at)


def _vector(buffer: bytes, table: int, slot: int, width: int) -> range:
    """The positions of a vector's ``width``-byte elements, checked to lie inside ``buffer`` as a whole."""
    start = _offset(buffer, table, slot)
    if start is None:
        return range(0)
    count = _read(buffer, "<I", start)
    if count * width > len(buffer) - start - 4:
        raise ArrowBodyError("the body's IPC metadata points outside itself")
    return range(start + 4, start + 4 + count * width, width)


def _aligned_as_lance_reads_it() -> pa.ipc.IpcReadOptions:
    """Every buffer at a 64-byte boundary, copying only those the body left short of one.

    IPC promises 8-byte alignment. pylance (arrow-rs) takes pyarrow's buffers in place and needs each at
    its type's own, and panics below it with a `PanicException`, which derives from BaseException:
    decimal128 after an int8 column lands at 8 mod 16 in a stream pyarrow itself writes (pyarrow
    25.0.0, pylance 12.0.0). `Alignment.DataTypeSpecific` leaves that decimal where it is.
    """
    import pyarrow as pa

    return pa.ipc.IpcReadOptions(ensure_alignment=pa.ipc.Alignment.At64Byte)


def _require_cap(max_bytes: object) -> None:
    """Checked before the refusal block, so a door's misconfigured cap is not reported as the caller's body."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError(f"max_bytes is a byte count, got {type(max_bytes).__name__}")
    if max_bytes < 1:
        raise ValueError(f"max_bytes must be at least 1, got {max_bytes}")


def _require_bytes(data: object) -> None:
    """pyarrow reads a ``str`` or a path-like as a file on disk; a caller's body is only ever bytes."""
    if not isinstance(data, bytes):
        raise TypeError(f"an Arrow body is decoded from bytes, got {type(data).__name__}")


@contextmanager
def _refused_as_arrow_body_error() -> Iterator[None]:
    # The block reads a caller's bytes and nothing else, and the body picks what it raises, so every
    # `Exception` in it is a refusal of the body. The door's cap is checked before the block, so a
    # misconfigured one is not blamed on the body, and the header walk's parser bounds-checks every read
    # it makes. pyarrow's own classes are only part of that: reading a body also runs each registered
    # extension type's `__arrow_ext_deserialize__` on metadata the caller chose, and those raise their
    # own classes — `import lance` registers `lance.blob.v2`, whose
    # deserializer raises TypeError for a storage type it refuses (pyarrow 25.0.0, pylance 12.0.0), in
    # every service that decodes a body. Pinned by
    # `packages/service-kit/tests/test_a_caller_arrow_body_is_validated_in_full.py`.
    try:
        yield
    except ArrowBodyError:
        raise
    except Exception as exc:
        raise ArrowBodyError(str(exc)) from exc


def _validated(table: pa.Table) -> pa.Table:
    table.validate(full=True)
    _decode_metadata(table.schema.metadata, "the schema's metadata")
    for index, field in enumerate(table.schema):
        _decode_field(field, f"column {index}")
    return table


def _decode_field(field: pa.Field, where: str) -> None:
    """Decode ``field``'s name and metadata, and every field nested under it, as UTF-8.

    The IPC format defines both as UTF-8 strings. ``validate(full=True)`` reads top-level column
    names and nothing below them, pyarrow hands metadata over as raw bytes, and pylance 12.0.0 raises
    an untyped ``ValueError`` writing a table whose metadata is not UTF-8. ``Field.name`` decodes
    strictly. ``where`` names the field by position, because its own name may be what is refused.
    """
    import pyarrow as pa

    try:
        name = field.name
    except UnicodeDecodeError as exc:
        raise ArrowBodyError(f"the name of {where} is not UTF-8") from exc
    _decode_metadata(field.metadata, f"the metadata of {where} ({name!r})")
    data_type = field.type
    while isinstance(data_type, (pa.DictionaryType, pa.BaseExtensionType)):
        data_type = data_type.value_type if isinstance(data_type, pa.DictionaryType) else data_type.storage_type
    for index in range(data_type.num_fields):
        _decode_field(data_type.field(index), f"child {index} of {where} ({name!r})")


def _decode_metadata(metadata: dict[bytes, bytes] | None, where: str) -> None:
    for key, value in (metadata or {}).items():
        try:
            key.decode("utf-8")
            value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArrowBodyError(f"{where} holds a key or value that is not UTF-8") from exc
