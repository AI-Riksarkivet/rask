"""Lance filter literals and ``IN`` lists, spelled once for every reader of a catalog table.

A filter string is SQL that Lance parses (`lance_docs/lance/guide/read_and_write.md` § Filter push-down), so a value
reaches it only as a quoted literal: a quote inside one would end the literal and let the rest of the value read as SQL.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Final


#: Values per ``IN`` list, so a wide set is read in bounded filters.
IN_LIST_SIZE: Final = 500


def sql_literal(value: str) -> str:
    """``value`` as a SQL string literal for a Lance filter: quoted, with every quote doubled."""
    return "'" + value.replace("'", "''") + "'"


def in_lists(column: str, values: Iterable[str]) -> Iterator[str]:
    """``<column> IN (...)`` filters over ``values`` in sorted order, at most :data:`IN_LIST_SIZE` values each."""
    keys = sorted(values)
    for start in range(0, len(keys), IN_LIST_SIZE):
        yield f"{column} IN ({', '.join(sql_literal(key) for key in keys[start : start + IN_LIST_SIZE])})"
