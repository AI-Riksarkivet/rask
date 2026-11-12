"""Read and edit a Lance manifest file's bytes (Lance ``protos/table.proto`` and ``protos/transaction.proto`` at v13.0.0).

pylance exposes neither a manifest's own ``version`` field, its ``next_row_id``, its ``max_fragment_id`` nor the
operation of a transaction it models no class for, and a manifest copied into another version slot must say that
slot's version, so the catalog reads and writes those fields itself. Every other byte is kept as it was.

The message is parsed by `lakehouse.manifest_wire`, which `lakehouse.features` reads the feature flags through, and a
message that does not parse is refused here as the bytes a caller handed the catalog.
"""

from __future__ import annotations

import secrets
import struct
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from lance_namespace import InvalidInputError

from lakehouse import manifest_wire


if TYPE_CHECKING:
    from collections.abc import Generator


#: A detached version id has the top bit set, as Lance's own detached commits do (``_versions/d<id>.manifest``).
_DETACHED_BIT: Final = 1 << 63


def detached_id() -> int:
    """A fresh detached version id, never one on the table's version line."""
    return _DETACHED_BIT | secrets.randbits(62)


#: A manifest file ends ``<u64 offset of the Manifest message> <u16 major> <u16 minor> LANC``, and the message at that
#: offset is ``<u32 length> <protobuf>`` (pylance 13.0.0 manifests).
_MAGIC: Final = b"LANC"
_FOOTER: Final = struct.Struct("<QHH4s")
_LENGTH: Final = struct.Struct("<I")


#: ``uint64 version = 3`` of ``message Manifest`` (Lance ``protos/table.proto`` at v13.0.0): a varint, wire type 0.
VERSION_FIELD: Final = 3

#: ``optional uint32 max_fragment_id = 11`` of ``message Manifest``: measured on pylance 13.0.0, 3 after three appends,
#: 4 after a one-fragment ``ReserveFragments``, and 4 still after the ``Rewrite`` that used it.
MAX_FRAGMENT_ID_FIELD: Final = 11

#: ``uint64 next_row_id = 14`` of ``message Manifest``: the first stable row id the table has not assigned. Measured on
#: pylance 13.0.0, no operation of Lance's own lowers it, a restore and an overwrite included.
NEXT_ROW_ID_FIELD: Final = 14

#: ``optional uint64 transaction_section = 23`` of ``message Manifest`` (Lance ``protos/table.proto`` at the commit
#: ``lance_docs/PROVENANCE.md`` pins): the file offset of the version's transaction, stored inline as
#: ``<u32 length><Transaction>`` (``lance_docs/lance/format/table/transaction.md`` § Inline Transaction Sections). Its
#: presence is tracked, so a section at offset 0 is written as 0, never left absent. pylance 13.0.0 does not write it; a
#: newer writer writes it and leaves :data:`DEPRECATED_TRANSACTION_SECTION_FIELD` absent.
TRANSACTION_SECTION_FIELD: Final = 23

#: ``transaction_section_deprecated = 21``, the legacy inline position, read only where field 23 is absent (the same
#: section). Measured on pylance 13.0.0, which writes it on every manifest (beside the ``_transactions/`` file it names in
#: field 12): 0 on an Append's, 56 on a CreateIndex's (its index section comes first); ``read_transaction`` reads this
#: copy, and reads the file when the manifest carries no field it knows.
DEPRECATED_TRANSACTION_SECTION_FIELD: Final = 21

#: The ``Transaction.operation`` oneof's field numbers pylance models no class for, by operation name. Measured on
#: pylance 13.0.0 transaction files: ``Append`` 100, ``Overwrite`` 102, ``CreateIndex`` 103, ``Rewrite`` 104 and
#: ``ReserveFragments`` 107; ``read_transaction`` answers the last with ``LanceOperation.BaseOperation``.
UNMODELLED_OPERATIONS: Final = {107: "ReserveFragments"}

#: The oneof's field numbers lie in this range; the transaction's own fields (``read_version``, ``uuid``, ``tag``,
#: ``transaction_properties``) are below it.
_OPERATION_FIELDS: Final = range(100, 200)


@contextmanager
def _parsed() -> Generator[None]:
    """Refuse a message :mod:`lakehouse.manifest_wire` cannot parse as the caller's input.

    Raises:
        InvalidInputError: The message ends inside a field or carries a wire type proto3 does not.
    """
    try:
        yield
    except manifest_wire.ManifestWireError as exc:
        raise InvalidInputError(f"the manifest does not parse: {exc}") from exc


def _with_version_field(message: bytes, version: int) -> bytes:
    """``message`` with its top-level field 3 (a varint) replaced by ``version``, every other field kept byte for byte.

    Raises:
        InvalidInputError: ``message`` does not parse.
    """
    out, at, written = bytearray(), 0, False
    while at < len(message):
        start = at
        with _parsed():
            number, value, at = manifest_wire.next_field(message, at)
        if number == VERSION_FIELD and isinstance(value, int):
            out += manifest_wire.encode_varint(VERSION_FIELD << 3) + manifest_wire.encode_varint(version)
            written = True
        else:
            out += message[start:at]
    if not written:
        out += manifest_wire.encode_varint(VERSION_FIELD << 3) + manifest_wire.encode_varint(version)
    return bytes(out)


def with_manifest_version(manifest: bytes, version: int) -> bytes:
    """``manifest`` with its ``version`` field set to ``version`` and every other byte's meaning unchanged.

    The sections before the Manifest message (index and transaction sections, addressed by absolute offsets) are kept
    byte for byte; only the message and the footer's offset to it are written again.

    Raises:
        InvalidInputError: ``manifest`` does not end in a Lance manifest footer.
    """
    if len(manifest) < _FOOTER.size or manifest[-4:] != _MAGIC:
        raise InvalidInputError("the staged manifest is not a Lance manifest (no LANC footer)")
    offset, major, minor, _ = _FOOTER.unpack(manifest[-_FOOTER.size :])
    body = _with_version_field(_length_prefixed(manifest, offset, section="Manifest message"), version)
    return manifest[:offset] + _LENGTH.pack(len(body)) + body + _FOOTER.pack(offset, major, minor, _MAGIC)


def _fields(message: bytes) -> list[tuple[int, int | bytes]]:
    """Every top-level varint and length-delimited field of ``message`` in order, with its value.

    Raises:
        InvalidInputError: ``message`` does not parse.
    """
    with _parsed():
        return manifest_wire.fields(message)


def varint_field(message: bytes, number: int) -> int | None:
    """The value of top-level varint field ``number`` in ``message``, or ``None`` when it is absent (proto3's zero).

    Raises:
        InvalidInputError: ``message`` does not parse.
    """
    with _parsed():
        return manifest_wire.varint_field(message, number)


#: ``google.protobuf.Timestamp timestamp = 7`` of ``message Manifest``: when the version was committed, as ``seconds = 1``
#: and ``nanos = 2``. Measured on pylance 13.0.0: equal to ``LanceDataset.versions()``'s ``timestamp`` for the version.
TIMESTAMP_FIELD: Final = 7


def commit_time(message: bytes) -> datetime | None:
    """When the version whose ``Manifest`` message is ``message`` was committed, in UTC; ``None`` when it records no time."""
    stamp = next((value for field, value in _fields(message) if field == TIMESTAMP_FIELD and isinstance(value, bytes)), None)
    if stamp is None:
        return None
    parts = {field: value for field, value in _fields(stamp) if isinstance(value, int)}
    return datetime.fromtimestamp(parts.get(1, 0), tz=UTC) + timedelta(microseconds=parts.get(2, 0) // 1000)


def transaction_section(manifest: bytes) -> bytes | None:
    """The ``Transaction`` message ``manifest`` carries inline, or ``None`` when it carries none.

    Read at :data:`TRANSACTION_SECTION_FIELD`, else at :data:`DEPRECATED_TRANSACTION_SECTION_FIELD`, as the format lets a
    reader take it from an old manifest.

    Raises:
        InvalidInputError: ``manifest`` is not a Lance manifest, or its section lies outside it.
    """
    message = manifest_message(manifest)
    offset = varint_field(message, TRANSACTION_SECTION_FIELD)
    if offset is None:
        offset = varint_field(message, DEPRECATED_TRANSACTION_SECTION_FIELD)
    if offset is None:
        return None
    return _length_prefixed(manifest, offset, section="transaction section")


def operation_name(transaction: bytes) -> str | None:
    """The operation a ``Transaction`` message carries when it is one pylance models no class for, else ``None``."""
    numbers = {field for field, _ in _fields(transaction) if field in _OPERATION_FIELDS}
    if len(numbers) != 1:
        return None
    (number,) = numbers
    return UNMODELLED_OPERATIONS.get(number)


def manifest_message(manifest: bytes) -> bytes:
    """The ``Manifest`` protobuf message ``manifest`` carries.

    Raises:
        InvalidInputError: ``manifest`` does not end in a Lance manifest footer, or the footer points outside it.
    """
    if len(manifest) < _FOOTER.size or manifest[-4:] != _MAGIC:
        raise InvalidInputError("not a Lance manifest (no LANC footer)")
    return _length_prefixed(manifest, _FOOTER.unpack(manifest[-_FOOTER.size :])[0], section="Manifest message")


def _length_prefixed(manifest: bytes, offset: int, *, section: str) -> bytes:
    """The ``<u32 length><bytes>`` section of ``manifest`` at ``offset``, every byte of it before the footer.

    Raises:
        InvalidInputError: The section begins or ends past the file's footer.
    """
    body_end = len(manifest) - _FOOTER.size
    if offset + _LENGTH.size > body_end:
        raise InvalidInputError(f"the manifest's {section} points outside the file")
    (length,) = _LENGTH.unpack_from(manifest, offset)
    start = offset + _LENGTH.size
    if start + length > body_end:
        raise InvalidInputError(f"the manifest's {section} runs past the file")
    return manifest[start : start + length]
