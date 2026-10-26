"""A create's ``lance-encoding:*`` knobs: judged against what Lance honours, stamped on the fields each acts on ([[LH-246]]).

Lance reads every encoding knob from a field's own metadata (`lance_docs/file_format.md` § Compression
Configuration), and the table keeps that metadata, so every later write to the table encodes under it. A value
Lance cannot honour is therefore refused 400 at the create, whether it rides the ``properties`` or the payload's
own field metadata, before anything is written. Measured on pylance 13.0.0: an empty create holding
``compression=bogus`` commits and every later append fails ``OSError: Unknown compression scheme``;
``dict-size-ratio=7`` or ``0`` raises pyo3's ``PanicException``, a ``BaseException`` that escapes every
``except Exception``, on the first low-cardinality page; and an out-of-range ``rle-threshold``, a level beside
lz4 or an unknown ``packed`` value is ignored without a word.

NOTHING IS SET BY DEFAULT (ADR 0066, [[LH-034]]): general compression runs after FSST, bitpacking and RLE, and on
values under ~1 KiB it costs more than it saves, so a workload that knows its own value sizes opts in per table.

WHICH FIELDS EACH KNOB ACTS ON, measured on pylance 13.0.0 by the encoded size with the knob at either end, or by
whether a bad value reaches the encoder. Where `file_format.md` names the types, the measurement is wider, and the
stamp follows the measurement:

* ``compression``, ``compression-level`` and ``general``: every leaf, boolean included.
* ``rle-threshold`` and the ``dict-*`` knobs: every leaf but a boolean. The doc lists RLE for fixed-width
  primitives; it also changes a string or binary column's size, through the dictionary's indices.
* ``bss``: the 32- and 64-bit integer, float and temporal leaves, not 16-bit and not decimal. The doc lists
  f32, f64 and timestamps; int32, int64, uint64, date32 and time64 shrink under it too.
* ``packed``: a struct, never a leaf. The doc says any string; Lance packs on ``true``, ``1``, ``yes`` or ``on``
  in any case and ignores every other value, so those and their negatives are what is accepted.

A leaf under a struct or a variable-size list reads its own metadata (measured: a bad scheme on a struct's child or
a list's item reaches the encoder, the same scheme on the parent does not). A fixed-size list's item is encoded
under no knob, on the list or on its item, so nothing is stamped there; a map, a union and a list view are not
descended, unmeasured.

``structural-encoding`` is refused: a forced layout overrides Lance's own per-page choice on every later write,
and ``sparse`` is an input error before file format 2.3 (`file_format.md` § Writer Selection) where the catalog
writes 2.2. A ``lance-encoding:*`` property the table above does not name is refused rather than stamped, since Lance
reads a misspelt knob as absent; on a payload field such a key is Lance's own (a blob column's thresholds) and is
left to Lance.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping

import pyarrow as pa
from lance_namespace import InvalidInputError
from pydantic import BaseModel, ConfigDict


PREFIX = "lance-encoding:"
_STRUCTURAL = PREFIX + "structural-encoding"
#: A level is honoured by zstd alone (`file_format.md` § Compression Level: lz4's mode is not exposed).
_ZSTD_LEVELS = range(23)
#: The integer and decimal forms Rust's ``str::parse`` reads; ``1_0``, ``inf`` and a padded value are refused.
_INTEGER = re.compile(r"[+-]?[0-9]+")
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_PACKED_VALUES = frozenset({"true", "1", "yes", "on", "false", "0", "no", "off"})


class Knob(BaseModel):
    """One ``lance-encoding:*`` knob: the values Lance honours, how a refusal names them, and the fields it acts on."""

    model_config = ConfigDict(frozen=True)

    accepts: Callable[[str], bool]
    expected: str
    acts_on: Callable[[pa.DataType], bool]
    #: The scheme knob a level knob is honoured beside, when that scheme is zstd.
    level_of: str | None = None


def _is_leaf(dtype: pa.DataType) -> bool:
    return not pa.types.is_nested(dtype)


def _is_dictionary_leaf(dtype: pa.DataType) -> bool:
    return _is_leaf(dtype) and not pa.types.is_boolean(dtype)


def _is_split_leaf(dtype: pa.DataType) -> bool:
    numeric = pa.types.is_integer(dtype) or pa.types.is_floating(dtype) or pa.types.is_temporal(dtype)
    return numeric and dtype.bit_width in (32, 64)


def _choice(values: Iterable[str], acts_on: Callable[[pa.DataType], bool]) -> Knob:
    allowed = frozenset(values)
    return Knob(accepts=allowed.__contains__, expected=f"one of {sorted(allowed)}", acts_on=acts_on)


def _is_integer(value: str) -> bool:
    return _INTEGER.fullmatch(value) is not None


def _is_divisor(value: str) -> bool:
    return _is_integer(value) and int(value) > 1


def _decimal(value: str) -> float | None:
    return float(value) if _DECIMAL.fullmatch(value) else None


def _is_ratio_from_zero(value: str) -> bool:
    ratio = _decimal(value)
    return ratio is not None and 0.0 <= ratio <= 1.0


def _is_ratio_above_zero(value: str) -> bool:
    ratio = _decimal(value)
    return ratio is not None and 0.0 < ratio <= 1.0


#: Each knob `file_format.md` § Compression Configuration defines, but ``structural-encoding``. The schemes are the
#: ones Lance's encoder resolves (measured: ``ZSTD``, ``gzip``, ``brotli`` and ``snappy`` are each "Unknown
#: compression scheme"; a dictionary's values take lz4, zstd or none).
_KNOBS: dict[str, Knob] = {
    "compression": _choice(("none", "fsst", "lz4", "zstd"), _is_leaf),
    "compression-level": Knob(accepts=_is_integer, expected="an integer", acts_on=_is_leaf, level_of="compression"),
    "general": _choice(("off", "on"), _is_leaf),
    "rle-threshold": Knob(accepts=_is_ratio_from_zero, expected="a decimal from 0 to 1", acts_on=_is_dictionary_leaf),
    "dict-divisor": Knob(accepts=_is_divisor, expected="an integer greater than 1", acts_on=_is_dictionary_leaf),
    "dict-size-ratio": Knob(accepts=_is_ratio_above_zero, expected="a decimal above 0 and at most 1", acts_on=_is_dictionary_leaf),
    "dict-values-compression": _choice(("none", "lz4", "zstd"), _is_dictionary_leaf),
    "dict-values-compression-level": Knob(accepts=_is_integer, expected="an integer", acts_on=_is_dictionary_leaf, level_of="dict-values-compression"),
    "bss": _choice(("off", "on", "auto"), _is_split_leaf),
    "packed": Knob(accepts=lambda value: value.lower() in _PACKED_VALUES, expected=f"one of {sorted(_PACKED_VALUES)} in any case", acts_on=pa.types.is_struct),
}


def require_valid_encoding(properties: Mapping[str, str] | None) -> dict[str, str]:
    """The ``lance-encoding:*`` subset of ``properties``, or a 400 naming the first knob Lance cannot honour."""
    encoding = {key: value for key, value in (properties or {}).items() if key.startswith(PREFIX)}
    for key, value in encoding.items():
        if key == _STRUCTURAL:
            raise InvalidInputError(f"{key} is not accepted: a forced layout overrides Lance's own choice on every later write to the table")
        knob = _KNOBS.get(key.removeprefix(PREFIX))
        if knob is None:
            raise InvalidInputError(f"{key} is not an encoding knob Lance defines; the knobs are {sorted(PREFIX + name for name in _KNOBS)}")
        if not knob.accepts(value):
            raise InvalidInputError(f"{key}={value!r} is not accepted: Lance takes {knob.expected}")
        if knob.level_of is not None:
            scheme = PREFIX + knob.level_of
            if encoding.get(scheme) != "zstd":
                raise InvalidInputError(f"{key} is accepted only beside {scheme}=zstd: no other scheme takes a level")
            if int(value) not in _ZSTD_LEVELS:
                raise InvalidInputError(f"{key}={value!r} is not accepted: zstd takes a level from 0 to 22")
    return encoding


def _fields_within(fields: Iterable[pa.Field]) -> Iterator[pa.Field]:
    for field in fields:
        yield field
        yield from _fields_within(field.type.field(i) for i in range(field.type.num_fields))


def require_valid_field_encoding(schema: pa.Schema) -> None:
    """Refuse 400 a payload field, at any depth, holding a compression knob Lance cannot honour.

    Only the knobs :data:`_KNOBS` names and ``structural-encoding`` are judged here. ``lance-encoding:`` is Lance's
    whole field-configuration prefix (`file_format.md` § Encoding Metadata): pylance's own ``blob_field`` writes a
    blob column's ``lance-encoding:blob-*`` thresholds there (`guide.md` § Blob), and those stay Lance's to judge.
    """
    for field in _fields_within(schema):
        metadata = {key.decode(errors="replace"): value.decode(errors="replace") for key, value in (field.metadata or {}).items()}
        require_valid_encoding({key: value for key, value in metadata.items() if key == _STRUCTURAL or key.removeprefix(PREFIX) in _KNOBS})


def _stamped(field: pa.Field, encoding: Mapping[str, str]) -> pa.Field:
    dtype = field.type
    if pa.types.is_struct(dtype):
        dtype = pa.struct([_stamped(child, encoding) for child in dtype])
    elif pa.types.is_list(dtype):
        dtype = pa.list_(_stamped(dtype.value_field, encoding))
    elif pa.types.is_large_list(dtype):
        dtype = pa.large_list(_stamped(dtype.value_field, encoding))
    knobs = {key: value for key, value in encoding.items() if _KNOBS[key.removeprefix(PREFIX)].acts_on(dtype)}
    stamped = field.with_type(dtype)
    return stamped.with_metadata({**(field.metadata or {}), **knobs}) if knobs else stamped


def stamp_encoding(table: pa.Table, properties: Mapping[str, str] | None) -> pa.Table:
    """``table`` with each judged ``lance-encoding:*`` property on every field it acts on, and on no other."""
    encoding = require_valid_encoding(properties)
    if not encoding:
        return table
    schema = pa.schema([_stamped(field, encoding) for field in table.schema], metadata=table.schema.metadata)
    return pa.Table.from_arrays(table.columns, schema=schema)
