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


def or_(*clauses: str) -> str:
    """OR-join the non-empty clauses, PARENTHESISING each one.

    The parens are the whole point and are not cosmetic: ``AND`` binds tighter than ``OR``,
    and every real caller here ORs together conjunctions built by :func:`and_` (a row key is
    ``doc = 'x' AND chunk = 1``). Joined bare, two keys render as::

        doc = 'a' AND chunk = 1 OR doc = 'b' AND chunk = 2

    which parses as ``(doc='a' AND chunk=1) OR (doc='b' AND chunk=2)`` only by luck of this
    shape — the moment a leg is a bare disjunction or a caller passes a single ``OR`` clause,
    the grouping silently changes and the predicate matches rows nobody asked for. A filter
    that returns the WRONG rows is worse than one that errors, because it looks like data.
    """
    return " OR ".join(f"({c})" for c in clauses if c)
