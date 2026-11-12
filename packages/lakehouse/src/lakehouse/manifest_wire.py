"""The protobuf wire format of a Lance manifest, read strictly: one reader for every plane that reads one by hand, and
the one varint encoder a plane that edits one writes with.

pylance exposes neither a manifest's feature flags nor the fields the catalog edits to finalize a staged version, so
``lakehouse.features`` and ``catalog.core.manifest_bytes`` read the ``Manifest`` message's bytes themselves. They read it
here, so a message that ends inside a field, or carries a wire type proto3 does not, is refused by both: a reader that
stopped there and answered what it had read so far would answer feature flags ``(0, 0)``, which every gate passes.
"""

from __future__ import annotations

from typing import Final


class ManifestWireError(ValueError):
    """A protobuf message that does not parse: it ends inside a field, or names a wire type proto3 does not carry."""


_VARINT: Final = 0
_LENGTH_DELIMITED: Final = 2

#: The fixed-width wire types and their widths. Group starts and ends (3 and 4) were removed from proto3 and 6 and 7 were
#: never assigned, so a message holding one is not read past it.
_FIXED_WIDTHS: Final = {1: 8, 5: 4}

#: A varint carries at most 64 bits, in ten bytes of seven.
_MAX_VARINT_SHIFT: Final = 63


def read_varint(buffer: bytes, at: int) -> tuple[int, int]:
    """The varint starting at ``at`` in ``buffer`` and the offset after it.

    Raises:
        ManifestWireError: ``buffer`` ends inside the varint, or the varint runs past 64 bits.
    """
    value, shift = 0, 0
    while True:
        if at >= len(buffer) or shift > _MAX_VARINT_SHIFT:
            raise ManifestWireError("a Lance protobuf message ends inside a varint")
        byte = buffer[at]
        value |= (byte & 0x7F) << shift
        at += 1
        if not byte & 0x80:
            return value, at
        shift += 7


def encode_varint(value: int) -> bytes:
    """``value`` as a protobuf varint, the encoding :func:`read_varint` reads.

    Raises:
        ValueError: ``value`` is negative, which a varint field of a Lance manifest never holds.
    """
    if value < 0:
        raise ValueError(f"a varint field of a Lance manifest holds no negative value, not {value}")
    out = bytearray()
    while True:
        low, value = value & 0x7F, value >> 7
        if not value:
            return bytes([*out, low])
        out.append(low | 0x80)


def next_field(message: bytes, at: int) -> tuple[int, int | bytes | None, int]:
    """The top-level field whose key starts at ``at``: its number, its value and the offset after it.

    A varint's value is its int and a length-delimited field's its bytes; a fixed-width field's is ``None``, since no
    reader here asks for one.

    Raises:
        ManifestWireError: The field runs past the message, or its wire type is not one proto3 carries.
    """
    key, at = read_varint(message, at)
    number, wire = key >> 3, key & 0x7
    if wire == _VARINT:
        value, at = read_varint(message, at)
        return number, value, at
    if wire == _LENGTH_DELIMITED:
        length, at = read_varint(message, at)
        return number, message[at : _within(message, at + length)], at + length
    if wire in _FIXED_WIDTHS:
        return number, None, _within(message, at + _FIXED_WIDTHS[wire])
    raise ManifestWireError(f"a Lance protobuf message carries wire type {wire}, which proto3 does not")


def _within(message: bytes, end: int) -> int:
    if end > len(message):
        raise ManifestWireError("a Lance protobuf message ends inside a field")
    return end


def fields(message: bytes) -> list[tuple[int, int | bytes]]:
    """Every top-level varint and length-delimited field of ``message`` in order, with its value.

    Raises:
        ManifestWireError: ``message`` does not parse (:func:`next_field`).
    """
    at, found = 0, []
    while at < len(message):
        number, value, at = next_field(message, at)
        if value is not None:
            found.append((number, value))
    return found


def varint_field(message: bytes, number: int) -> int | None:
    """The value of top-level varint field ``number`` in ``message``, or ``None`` when it is absent (proto3's zero).

    Raises:
        ManifestWireError: ``message`` does not parse (:func:`next_field`).
    """
    found = None
    for field, value in fields(message):
        if field == number and isinstance(value, int):
            found = value
    return found
