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
Every service that reads a caller's Arrow bytes reads them through this module — the catalog's write
doors through :func:`decode_arrow_stream`, the annotator's import through
:func:`decode_arrow_stream_or_file`; pinned by
``tests/unit/test_a_caller_arrow_body_is_decoded_by_the_validating_decoder.py``.

``pyarrow`` is imported inside the functions, not at module scope, so a module that only needs
:data:`ARROW_STREAM_MEDIA_TYPE` (a header value on a publish path that keeps the heavy import off its
own import time) can take the constant without pulling pyarrow.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterator

    import pyarrow as pa


#: The Arrow-IPC **stream** media type — the ``Content-Type`` every catalog write body carries and the
#: type the ``/points`` / annotations read responses are served as. One spelling, one import.
ARROW_STREAM_MEDIA_TYPE = "application/vnd.apache.arrow.stream"

#: The IPC file format's leading magic. A stream opens with a message length instead, so the framing
#: of a body is read from its first six bytes rather than guessed by trying one reader after another.
_FILE_MAGIC = b"ARROW1"


class ArrowBodyError(ValueError):
    """Bytes that are not a valid Arrow IPC body — in their framing, buffers, names, metadata, or an
    extension type's own metadata.

    A ``ValueError`` rather than a response type: each door maps it to its own refusal (the catalog to
    ``InvalidInputError``, the annotator's import to ``ValidationError``). The message is the reader's,
    which names what is at fault and never the memory it refused to read; the reader's exception is
    its ``__cause__``.
    """


def encode_arrow_stream(table: pa.Table) -> bytes:
    """Serialize ``table`` as an Arrow-IPC stream. Pass ``schema.empty_table()`` to encode an
    empty, correctly-typed body (the shape a ``create`` sends)."""
    import pyarrow as pa

    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def decode_arrow_stream(data: bytes) -> pa.Table:
    """A caller's Arrow IPC **stream** as a table validated in full, or :class:`ArrowBodyError`.

    Read whole, because a stream cut inside a batch parses its schema and fails only on the batch, and
    refused unless the stream is the whole body: a reader stops at the first end-of-stream marker, so
    a second stream or trailing bytes would be dropped while the door answers for the rest.

    Raises:
        TypeError: ``data`` is not bytes.
        ArrowBodyError: the body is not a valid Arrow IPC stream.
    """
    import pyarrow as pa

    _require_bytes(data)
    with _refused_as_arrow_body_error():
        source = pa.BufferReader(data)
        table = _validated(pa.ipc.open_stream(source, options=_aligned_as_lance_reads_it()).read_all())
        if (unread := len(data) - source.tell()) > 0:
            raise ArrowBodyError(f"{unread} bytes follow the end of the stream")
        return table


def decode_arrow_stream_or_file(data: bytes) -> pa.Table:
    """:func:`decode_arrow_stream`, for a door that also takes the IPC **file** framing.

    Both are what pyarrow writes depending on which writer a caller reached for; the file magic tells
    them apart, so a stream whose buffers fail validation is refused for its buffers, not re-read as a
    file and refused for the wrong reason.

    Raises:
        TypeError: ``data`` is not bytes.
        ArrowBodyError: the body is not a valid Arrow IPC stream or file.
    """
    import pyarrow as pa

    _require_bytes(data)
    if not data.startswith(_FILE_MAGIC):
        return decode_arrow_stream(data)
    with _refused_as_arrow_body_error():
        return _validated(pa.ipc.open_file(data, options=_aligned_as_lance_reads_it()).read_all())


def _aligned_as_lance_reads_it() -> pa.ipc.IpcReadOptions:
    """Every buffer at a 64-byte boundary, copying only those the body left short of one.

    IPC promises 8-byte alignment. pylance (arrow-rs) takes pyarrow's buffers in place and needs each at
    its type's own, and panics below it with a `PanicException`, which derives from BaseException:
    decimal128 after an int8 column lands at 8 mod 16 in a stream pyarrow itself writes (pyarrow
    25.0.0, pylance 12.0.0). `Alignment.DataTypeSpecific` leaves that decimal where it is.
    """
    import pyarrow as pa

    return pa.ipc.IpcReadOptions(ensure_alignment=pa.ipc.Alignment.At64Byte)


def _require_bytes(data: object) -> None:
    """pyarrow reads a ``str`` or a path-like as a file on disk; a caller's body is only ever bytes."""
    if not isinstance(data, bytes):
        raise TypeError(f"an Arrow body is decoded from bytes, got {type(data).__name__}")


@contextmanager
def _refused_as_arrow_body_error() -> Iterator[None]:
    # The block reads a caller's bytes and nothing else, and the body picks what it raises, so every
    # `Exception` in it is a refusal of the body. pyarrow's own classes are only part of that: reading a
    # body also runs each registered extension type's `__arrow_ext_deserialize__` on metadata the
    # caller chose, and those raise their own classes — `import lance` registers `lance.blob.v2`, whose
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
