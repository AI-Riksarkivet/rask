"""Lance filters, spelled once for every reader of a catalog table.

A filter string is SQL that Lance parses (`lance_docs/lance/guide/read_and_write.md` § Filter push-down), so a value
reaches it only as a quoted literal: a quote inside one would end the literal and let the rest of the value read as SQL.
A filter is written as a template (`lance_filter`), whose every interpolation is a value and is spelled as a literal,
so a value cannot reach the filter as SQL by being left unquoted.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from string.templatelib import Interpolation, Template
from typing import Final


__all__ = [
    "in_lists",
    "lance_filter",
]


#: Values per ``IN`` list, so a wide set is read in bounded filters.
_IN_LIST_SIZE: Final = 500


def lance_filter(template: Template) -> str:
    """The filter ``template`` spells: its text as written, and each value it interpolates as a literal.

    A ``str`` is quoted with every quote doubled, an ``int`` is its digits, and a non-empty list or tuple of strings is
    the literals of an ``IN`` list. Doubling is the whole rule: measured on pylance 14.0.3 (2026-11-25), a backslash is
    no escape, and a value ending in one, holding a newline, or spelling ``x' OR '1'='1`` matches only itself. A column,
    a function or a type is the template's text, never an interpolation.

    Raises:
        TypeError: A value of any other type, a ``bool`` included (it would read as ``True``), or a list holding one.
        ValueError: An interpolation with a conversion or a format spec, which would spell the value before it is
            quoted, or an empty list, which no ``IN`` takes.
    """
    return "".join(part if isinstance(part, str) else _literal(part) for part in template)


def in_lists(column: str, values: Iterable[str]) -> Iterator[str]:
    """``<column> IN (...)`` filters over ``values`` in sorted order, at most :data:`_IN_LIST_SIZE` values each, each
    rendered by `lance_filter` with the list as its one value; ``column`` is the caller's text, never a value."""
    keys = sorted(values)
    for start in range(0, len(keys), _IN_LIST_SIZE):
        yield lance_filter(Template(f"{column} IN (", Interpolation(keys[start : start + _IN_LIST_SIZE], "keys"), ")"))


def _literal(part: Interpolation) -> str:
    named = f"{{{part.expression}}}"
    if part.conversion is not None or part.format_spec:
        raise ValueError(f"a filter value takes no conversion or format spec: {named}")
    match part.value:
        case bool():
            raise TypeError(f"a filter value is a str, an int or a list of str, not a bool: {named}")
        case str() as text:
            return _sql_literal(text)
        case int() as number:
            return str(number)
        case list() | tuple() as values:
            if not values:
                raise ValueError(f"an IN list names at least one value: {named}")
            if not all(isinstance(value, str) for value in values):
                raise TypeError(f"an IN list holds strings only: {named}")
            return ", ".join(_sql_literal(value) for value in values)
        case other:
            raise TypeError(f"a filter value is a str, an int or a list of str, not {type(other).__name__}: {named}")


def _sql_literal(value: str) -> str:
    """``value`` as a SQL string literal for a Lance filter: quoted, with every quote doubled."""
    return "'" + value.replace("'", "''") + "'"
