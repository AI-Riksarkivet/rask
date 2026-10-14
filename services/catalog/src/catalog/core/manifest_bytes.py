"""Read and edit a Lance manifest file's bytes (Lance ``protos/table.proto`` at v13.0.0).

pylance exposes neither a manifest's own ``version`` field nor its ``next_row_id``, and a manifest copied into another
version slot must say that slot's version, so the catalog reads and writes those varints itself. Every other byte is
kept as it was.
"""

from __future__ import annotations

import secrets
import struct
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
    if offset + _LENGTH.size > len(manifest) - _FOOTER.size:
        raise InvalidInputError("the staged manifest's footer points outside the file")
    (length,) = _LENGTH.unpack_from(manifest, offset)
    body = _with_version_field(manifest[offset + _LENGTH.size : offset + _LENGTH.size + length], version)
    return manifest[:offset] + _LENGTH.pack(len(body)) + body + _FOOTER.pack(offset, major, minor, _MAGIC)


def varint_field(message: bytes, number: int) -> int | None:
    """The value of top-level varint field ``number`` in ``message``, or ``None`` when it is absent (proto3's zero)."""
    at, found = 0, None
    while at < len(message):
        key, at = _read_varint(message, at)
        wire = key & 0x7
        if wire == 0:
            value, at = _read_varint(message, at)
            if key >> 3 == number:
                found = value
        elif wire == 1:
            at += 8
        elif wire == 2:
            length, at = _read_varint(message, at)
            at += length
        elif wire == 5:
            at += 4
        else:
            raise InvalidInputError(f"a Manifest message has an unsupported wire type {wire}")
    return found


def manifest_message(manifest: bytes) -> bytes:
    """The ``Manifest`` protobuf message ``manifest`` carries.

    Raises:
        InvalidInputError: ``manifest`` does not end in a Lance manifest footer, or the footer points outside it.
    """
    if len(manifest) < _FOOTER.size or manifest[-4:] != _MAGIC:
        raise InvalidInputError("not a Lance manifest (no LANC footer)")
    offset = _FOOTER.unpack(manifest[-_FOOTER.size :])[0]
    if offset + _LENGTH.size > len(manifest) - _FOOTER.size:
        raise InvalidInputError("the manifest's footer points outside the file")
    (length,) = _LENGTH.unpack_from(manifest, offset)
    return manifest[offset + _LENGTH.size : offset + _LENGTH.size + length]
