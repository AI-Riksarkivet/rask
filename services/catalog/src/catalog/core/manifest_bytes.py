"""Read and edit a Lance manifest file's bytes (Lance ``protos/table.proto`` and ``protos/transaction.proto`` at v13.0.0).

pylance exposes neither a manifest's own ``version`` field, its ``next_row_id``, its ``max_fragment_id`` nor the
operation of a transaction it models no class for, and a manifest copied into another version slot must say that
slot's version, so the catalog reads and writes those fields itself. Every other byte is kept as it was.
"""

from __future__ import annotations

import secrets
import struct
from datetime import UTC, datetime, timedelta
from typing import Final

from lance_namespace import InvalidInputError


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

#: ``optional uint64 transaction_section = 21`` of ``message Manifest``: the file offset of the version's transaction,
#: stored inline as ``<u32 length><Transaction>``. Measured on pylance 13.0.0: 0 on an Append's manifest, 56 on a
#: CreateIndex's (its index section comes first); ``read_transaction`` reads this copy, not the ``_transactions/`` file.
TRANSACTION_SECTION_FIELD: Final = 21

#: The ``Transaction.operation`` oneof's field numbers pylance models no class for, by operation name. Measured on
#: pylance 13.0.0 transaction files: ``Append`` 100, ``Overwrite`` 102, ``CreateIndex`` 103, ``Rewrite`` 104 and
#: ``ReserveFragments`` 107; ``read_transaction`` answers the last with ``LanceOperation.BaseOperation``.
UNMODELLED_OPERATIONS: Final = {107: "ReserveFragments"}

#: The oneof's field numbers lie in this range; the transaction's own fields (``read_version``, ``uuid``, ``tag``,
#: ``transaction_properties``) are below it.
_OPERATION_FIELDS: Final = range(100, 200)


def _read_varint(buffer: bytes, at: int) -> tuple[int, int]:
    """The varint at ``at`` and the offset after it."""
    value, shift = 0, 0
    while True:
        if at >= len(buffer) or shift > 63:
            raise InvalidInputError("the staged manifest's Manifest message is truncated")
        byte = buffer[at]
        value |= (byte & 0x7F) << shift
        at += 1
        if not byte & 0x80:
            return value, at
        shift += 7


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if not value:
            out.append(low)
            return bytes(out)
        out.append(low | 0x80)


def _with_version_field(message: bytes, version: int) -> bytes:
    """``message`` with its top-level field 3 (a varint) replaced by ``version``, every other field kept byte for byte."""
    out, at, written = bytearray(), 0, False
    while at < len(message):
        start = at
        key, at = _read_varint(message, at)
        number, wire = key >> 3, key & 0x7
        if wire == 0:
            _, at = _read_varint(message, at)
        elif wire == 1:
            at += 8
        elif wire == 2:
            length, at = _read_varint(message, at)
            at += length
        elif wire == 5:
            at += 4
        else:
            raise InvalidInputError(f"the staged manifest's Manifest message has an unsupported wire type {wire}")
        if at > len(message):
            raise InvalidInputError("the staged manifest's Manifest message is truncated")
        if number == VERSION_FIELD and wire == 0:
            out += _varint(key) + _varint(version)
            written = True
        else:
            out += message[start:at]
    if not written:
        out += _varint(VERSION_FIELD << 3) + _varint(version)
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
    """Every top-level field of ``message`` in order: a varint's value, or a length-delimited field's bytes."""
    at, found = 0, []
    while at < len(message):
        key, at = _read_varint(message, at)
        number, wire = key >> 3, key & 0x7
        if wire == 0:
            value, at = _read_varint(message, at)
            found.append((number, value))
        elif wire == 1:
            at += 8
        elif wire == 2:
            length, at = _read_varint(message, at)
            if at + length > len(message):
                raise InvalidInputError("a Lance protobuf message is truncated")
            found.append((number, message[at : at + length]))
            at += length
        elif wire == 5:
            at += 4
        else:
            raise InvalidInputError(f"a Lance protobuf message has an unsupported wire type {wire}")
    return found


def varint_field(message: bytes, number: int) -> int | None:
    """The value of top-level varint field ``number`` in ``message``, or ``None`` when it is absent (proto3's zero)."""
    found = None
    for field, value in _fields(message):
        if field == number and isinstance(value, int):
            found = value
    return found


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

    Raises:
        InvalidInputError: ``manifest`` is not a Lance manifest, or its section lies outside it.
    """
    offset = varint_field(manifest_message(manifest), TRANSACTION_SECTION_FIELD)
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
