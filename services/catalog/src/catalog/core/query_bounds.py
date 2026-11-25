"""A caller's expression held to a bound before Lance plans it.

Lance's planner recurses once per connective of a flat boolean chain, and the native query path answers a projection
whose size the caller chooses: each bound here refuses the request, as the caller's mistake, before Lance sees it.
"""

import re
from typing import Final

import lance
import pyarrow as pa
from lance_namespace import (
    InvalidInputError,
)

from catalog.core.lance_refusals import caller_sql


__all__ = [
    "MAX_CALLER_SQL_CONNECTIVES",
    "refuse_an_unbounded_boolean_chain",
    "refuse_an_unbounded_result",
]


#: The most AND/OR connectives a caller's SQL fragment may carry. Lance's planner recurses once per
#: connective of a flat boolean chain with no bound: on pylance 12.0.0, 150,000 `id = n OR ...` terms end
#: the process with SIGSEGV. Its parser already bounds nesting ("recursion limit exceeded"), and a large
#: value set carries no connective as one `IN (...)` list, so the bound sits far below the crash and above
#: any hand-written filter.
MAX_CALLER_SQL_CONNECTIVES: Final = 1_000


_QUOTED_SQL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`")


_CONNECTIVE = re.compile(r"\b(?:and|or)\b", re.IGNORECASE)


def refuse_an_unbounded_boolean_chain(sql: str | None, *, field: str) -> None:
    """Refuse a caller's SQL fragment whose AND/OR chain could exhaust Lance's planner, before Lance sees it.

    Connectives inside quoted literals and identifiers are not counted: they are text, not structure.

    Raises:
        TypeError: ``sql`` is not a string.
        InvalidInputError: ``sql`` joins more than ``MAX_CALLER_SQL_CONNECTIVES`` conditions.
    """
    if sql is None:
        return
    if not isinstance(sql, str):
        raise TypeError(f"{field} must be a SQL string, got {type(sql).__name__}")
    connectives = len(_CONNECTIVE.findall(_QUOTED_SQL.sub(" ", sql)))
    if connectives > MAX_CALLER_SQL_CONNECTIVES:
        raise InvalidInputError(
            f"{field} joins {connectives} conditions with AND/OR; at most {MAX_CALLER_SQL_CONNECTIVES} are accepted — "
            "pass a large set of values as one IN (...) list"
        )


#: The native query path carries `k` and `offset` as u32: MEASURED on pylance 12.0.0, `k=2**32` and
#: `offset=10**12` raise `OverflowError` inside the native call, which answered 500.
_U32_MAX: Final = 2**32 - 1


#: Rows read to estimate a variable-width column's width, when the projection carries one.
_WIDTH_SAMPLE_ROWS: Final = 16


#: What the native answer adds beyond the projection: `_distance` or `_score` (float32) and `_rowid` (u64).
_ANSWER_EXTRA_BYTES: Final = 16


def refuse_an_unbounded_result(dataset: lance.LanceDataset, *, k: int, offset: int | None, columns: object, max_bytes: int) -> None:
    """Refuse a native query whose answer could exceed ``max_bytes``, before the native backend reads a row ([[LH-247]]).

    The native backend materialises the whole answer in one buffer before the first byte is sent, so
    the only bound this door has is the row count it asks for. `k` has a minimum of 0 and no maximum in
    the spec (`spec.yaml` QueryTableRequest), and `k=0` answers EVERY row. MEASURED on pylance 12.0.0
    from outside a one-CPU catalog process, 164 MB table of 1024-dim float32 vectors on a moto store:
    `k=0` returned 169,290,994 bytes and peaked at 822,192 kB against the pod's 524,288 kB limit, and a
    68 MB answer (`k=16000`, 492 MB table) at 885,188 kB. A whole-table read belongs to `/changes` or a
    credential vend, which stream.

    `k` is capped by the projected row width: exact for fixed-width columns, and estimated from
    :data:`_WIDTH_SAMPLE_ROWS` rows of the projection when it carries a variable-width one. `offset` is
    not, so a reader pages a large read with a bounded `k` (`lineage.services.catalog_reader`): the rows an
    offset skips are streamed past, not held. MEASURED on the same table and process: `k=1000,
    offset=39000` (skipping 156 MB) peaked at 395,256 kB, beside 381,412 kB for `k=4000` with no offset.

    Raises:
        InvalidInputError: ``k`` is 0, ``k`` or ``offset`` exceeds u32, or ``k`` rows exceed ``max_bytes``.
    """
    skip = offset or 0
    if k == 0:
        raise InvalidInputError("k must be at least 1: k=0 asks for every row in one answer; read a whole table through /changes or a credential vend")
    for name, value in (("k", k), ("offset", skip)):
        if value > _U32_MAX:
            raise InvalidInputError(f"{name}={value} exceeds {_U32_MAX}, the largest row count a query can carry")
    row_bytes = _projected_row_bytes(dataset, columns)
    ceiling = max(1, max_bytes // row_bytes)
    if k > ceiling:
        raise InvalidInputError(
            f"k = {k} rows of about {row_bytes} bytes exceed this door's {max_bytes}-byte answer (at most {ceiling} rows); "
            "page with a smaller k and an offset, or read a whole table through /changes or a credential vend"
        )


def _projected_row_bytes(dataset: lance.LanceDataset, columns: object) -> int:
    """The in-memory bytes one native answer row of the request's ``columns`` holds, its own extra columns included.

    Exact for fixed-width columns; a variable-width column's width is sampled from the table.
    """
    names = getattr(columns, "column_names", None)
    aliases = getattr(columns, "column_aliases", None)
    projection: list[str] | dict[str, str] | None = names or aliases or None
    with caller_sql("invalid query columns"):
        schema = dataset.scanner(columns=projection, limit=1).projected_schema
        widths = [_fixed_width(field.type) for field in schema]
        variable = [i for i, width in enumerate(widths) if width is None]
        sampled = 0
        if variable:
            sample = dataset.scanner(columns=_narrowed(projection, schema, variable), limit=_WIDTH_SAMPLE_ROWS).to_table()
            sampled = -(-sample.nbytes // sample.num_rows) if sample.num_rows else 0
    return sum(width for width in widths if width is not None) + sampled + _ANSWER_EXTRA_BYTES


def _narrowed(projection: list[str] | dict[str, str] | None, schema: pa.Schema, positions: list[int]) -> list[str] | dict[str, str]:
    """``projection`` cut down to the output columns at ``positions``, in the form it was given.

    A projected schema lists its fields in the projection's order, so a position names the same column
    in both; the projection's own spelling (a nested field path, an alias) is what the scanner takes back.
    """
    if isinstance(projection, dict):
        items = list(projection.items())
        return dict(items[i] for i in positions)
    if projection:
        return [projection[i] for i in positions]
    return [schema.field(i).name for i in positions]


def _fixed_width(data_type: pa.DataType) -> int | None:
    """The in-memory bytes one value of ``data_type`` takes, or ``None`` when that depends on the value."""
    if pa.types.is_fixed_size_list(data_type):
        inner = _fixed_width(data_type.value_type)
        return None if inner is None else inner * data_type.list_size
    if pa.types.is_struct(data_type):
        widths = [_fixed_width(data_type.field(i).type) for i in range(data_type.num_fields)]
        return None if None in widths else sum(w for w in widths if w is not None)
    try:
        return max(1, data_type.bit_width // 8)
    except ValueError:
        return None
