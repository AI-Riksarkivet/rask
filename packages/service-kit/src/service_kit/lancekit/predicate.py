"""Shared SQL-predicate composition — ONE quoting contract for every seam.

Predicates are *strings* across all three planes (pylance ``filter=``, the writer
seam's ``delete(predicate)``, and the lance-ns catalog REST wire), so this is pure
string rendering, not an expression tree (docs/LANCEDB_SDK_AUDIT.md §2 — the SDK's
``Expr`` would flatten back to a string at every one of those seams). Values are
escaped by doubling single quotes at render time; field names are trusted config
(descriptor bindings), never user input.
"""

from collections.abc import Iterable


def quote_literal(value: object) -> str:
    """Render a Python value as a SQL literal — strings quoted + escaped, numbers
    and booleans bare. The single injection boundary for interpolated values."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def eq(field: str, value: object) -> str:
    """``field = <literal>``."""
    return f"{field} = {quote_literal(value)}"


def ne(field: str, value: object) -> str:
    """``field != <literal>``."""
    return f"{field} != {quote_literal(value)}"


def isin(field: str, values: Iterable[object]) -> str:
    """``field IN (<literals>)`` — deduped + sorted so the predicate is deterministic
    regardless of input order (stable across retries, cache-friendly).

    Raises ``ValueError`` on an empty iterable: ``IN ()`` is invalid SQL at every
    consuming seam, so emptiness must be handled by the caller (skip the clause)."""
    rendered = sorted({quote_literal(v) for v in values})
    if not rendered:
        raise ValueError("isin() requires at least one value")
    return f"{field} IN ({', '.join(rendered)})"


def and_(*clauses: str) -> str:
    """AND-join the non-empty clauses (empties dropped, so callers can pass
    optional legs unconditionally)."""
    return " AND ".join(c for c in clauses if c)
