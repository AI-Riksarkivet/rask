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
reads a misspelt knob as absent. A blob-v2 column's two placement thresholds are judged on its own field, as Lance
judges them; any other key on a field is Lance's own.

EVERY DOOR THAT WRITES FIELD METADATA asks the same rule: the create door of its properties and payload fields, and
every commit, a catalog door's (``dataplane.commit_stamped``) and a stock client's (``version/create``), of the keys it
adds or changes (:func:`require_no_new_invalid_encoding`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import TYPE_CHECKING

import pyarrow as pa
from lance_namespace import InvalidInputError
from pydantic import BaseModel, ConfigDict


if TYPE_CHECKING:
    import lance
    from lance.lance.schema import LanceField


PREFIX = "lance-encoding:"
_STRUCTURAL = PREFIX + "structural-encoding"
#: A level is honoured by zstd alone (`file_format.md` § Compression Level: lz4's mode is not exposed).
_ZSTD_LEVELS = range(23)
#: The integer and decimal forms Rust's ``str::parse`` reads; ``1_0``, ``inf`` and a padded value are refused.
_INTEGER = re.compile(r"[+-]?[0-9]+")
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
PACKED = PREFIX + "packed"
#: The ``packed`` values Lance 2.1 and later pack on, in any case; any other value leaves the struct's leaves as columns.
_PACKING = frozenset({"true", "1", "yes", "on"})
_PACKED_VALUES = _PACKING | {"false", "0", "no", "off"}
#: A blob-v2 column's placement thresholds (`guide.md` § Blob: ``blob_field(..., inline_size_threshold=...,
#: dedicated_size_threshold=...)``). Lance takes each as "a non-negative integer that fits in usize" and refuses any
#: other on write (pylance 13.0.0, ``blob.rs:108``), as an ``OSError`` a door would answer 500.
_BLOB_THRESHOLDS = frozenset({PREFIX + "blob-inline-size-threshold", PREFIX + "blob-dedicated-size-threshold"})
_UNSIGNED = re.compile(r"[0-9]+")
_USIZE_MAX = 2**64 - 1


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


def packs(field: pa.Field) -> bool:
    """Whether a 2.1 or 2.2 file holds ``field`` as one packed column: its ``packed`` value is one Lance reads as true."""
    return (field.metadata or {}).get(PACKED.encode(), b"").decode(errors="replace").lower() in _PACKING


def packs_at_2_0(field: pa.Field) -> bool:
    """Whether a 2.0 file holds ``field`` as one packed column: it carries any ``packed`` value, ``false`` included."""
    return PACKED.encode() in (field.metadata or {})


def _judge(key: str, value: str, encoding: Mapping[str, str]) -> None:
    """Refuse 400 one judged key whose value Lance cannot honour; ``encoding`` is its field's (or properties') knobs."""
    if key == _STRUCTURAL:
        raise InvalidInputError(f"{key} is not accepted: a forced layout overrides Lance's own choice on every later write to the table")
    if key in _BLOB_THRESHOLDS:
        if _UNSIGNED.fullmatch(value) is None or int(value) > _USIZE_MAX:
            raise InvalidInputError(f"{key}={value!r} is not accepted: Lance takes a non-negative integer")
        return
    knob = _KNOBS[key.removeprefix(PREFIX)]
    if not knob.accepts(value):
        raise InvalidInputError(f"{key}={value!r} is not accepted: Lance takes {knob.expected}")
    if knob.level_of is not None:
        scheme = PREFIX + knob.level_of
        if encoding.get(scheme) != "zstd":
            raise InvalidInputError(f"{key} is accepted only beside {scheme}=zstd: no other scheme takes a level")
        if int(value) not in _ZSTD_LEVELS:
            raise InvalidInputError(f"{key}={value!r} is not accepted: zstd takes a level from 0 to 22")


def _is_judged(key: str) -> bool:
    return key == _STRUCTURAL or key in _BLOB_THRESHOLDS or (key.startswith(PREFIX) and key.removeprefix(PREFIX) in _KNOBS)


def require_valid_encoding(properties: Mapping[str, str] | None) -> dict[str, str]:
    """The ``lance-encoding:*`` subset of create ``properties``, or a 400 naming the first knob Lance cannot honour.

    A property names a knob to stamp, so one the knob table does not name is refused: Lance reads a misspelt knob as
    absent, and a blob column's thresholds are its own field's, never a table-wide stamp.
    """
    encoding = {key: value for key, value in (properties or {}).items() if key.startswith(PREFIX)}
    for key, value in encoding.items():
        if key != _STRUCTURAL and key.removeprefix(PREFIX) not in _KNOBS:
            raise InvalidInputError(f"{key} is not an encoding knob Lance defines; the knobs are {sorted(PREFIX + name for name in _KNOBS)}")
        _judge(key, value, encoding)
    return encoding


def _judged_keys(field: pa.Field) -> dict[str, str]:
    decoded = {key.decode(errors="replace"): value.decode(errors="replace") for key, value in (field.metadata or {}).items()}
    return {key: value for key, value in decoded.items() if _is_judged(key)}


def _fields_within(fields: Iterable[pa.Field]) -> Iterator[pa.Field]:
    for field in fields:
        yield field
        yield from _fields_within(field.type.field(i) for i in range(field.type.num_fields))


def require_valid_field_encoding(schema: pa.Schema) -> None:
    """Refuse 400 a payload field, at any depth, holding a knob or a blob threshold Lance cannot honour.

    Any other ``lance-encoding:`` key stays Lance's (`file_format.md` § Encoding Metadata calls the prefix its whole
    field configuration). The blob-v1 marker ``lance-encoding:blob`` is one: `guide.md` § Blob says a 2.2 write rejects
    it, and pylance 13.0.0 writes the column as blob v2 instead (measured), so it poisons nothing.
    """
    for field in _fields_within(schema):
        encoding = _judged_keys(field)
        for key, value in encoding.items():
            _judge(key, value, encoding)


def _encoding_by_field_id(dataset: lance.LanceDataset) -> dict[int, dict[str, str]]:
    """Each field's judged keys by its Lance field id, which a rename keeps and a re-type re-mints with no metadata."""
    found: dict[int, dict[str, str]] = {}

    def visit(field: LanceField, declared: pa.Field | None) -> None:
        found[field.id()] = _judged_keys(declared) if declared is not None else {}
        dtype = declared.type if declared is not None else None
        # A blob-v2 column is an Arrow extension type with no children of its own, over the struct Lance lists.
        storage = dtype.storage_type if isinstance(dtype, pa.ExtensionType) else dtype
        named = {storage.field(i).name: storage.field(i) for i in range(storage.num_fields)} if storage is not None else {}
        for child in field.children():
            visit(child, named.get(child.name()))

    for field in dataset.lance_schema.fields():
        visit(field, dataset.schema.field(field.name()))
    return found


def require_no_new_invalid_encoding(before: lance.LanceDataset | None, after: lance.LanceDataset) -> None:
    """Refuse 400 a commit that adds or changes a field's knob or blob threshold to one Lance cannot honour.

    Every later write encodes under the field metadata a commit leaves (measured on pylance 13.0.0: a commit stamping
    ``compression=bogus`` through ``update_field_metadata`` fails every append after it), so the commit is refused
    before its finalize. Only what the commit adds or changes is judged, by Lance field id: a knob a field already held
    stays the table's, and a rename, which keeps the id, adds nothing.
    """
    held = _encoding_by_field_id(before) if before is not None else {}
    for field_id, encoding in _encoding_by_field_id(after).items():
        was = held.get(field_id, {})
        for key, value in encoding.items():
            if was.get(key) != value:
                _judge(key, value, encoding)


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
