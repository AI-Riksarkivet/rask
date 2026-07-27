"""Descriptor-driven WHERE composition + value escaping — pure string logic.

Ported from ``backend.search.filters``, generalized: instead of five hardcoded
corpus fields, the filterable field names come from the descriptor's
``search.filterable`` list and compile to equality clauses. The values are
user-supplied query params, so the quote escaping is the injection boundary; the
field names are trusted config (the descriptor), never user input. ``raw`` is
the deliberate exception — user SQL ANDed in verbatim.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from service_kit.lancekit.predicate import eq


# The one filterable with non-equality semantics: a topic name exact-matches ANY
# of the row table's nested topic layer columns (``topic_l0`` … ``topic_lN``),
# so a treemap node at any depth filters the rows tagged with that name.
TOPIC_FILTER = "topic"
_TOPIC_LAYER_PREFIX = "topic_l"


def topic_layer_columns(schema_names: Sequence[str]) -> list[str]:
    """``topic_l0 … topic_l{N-1}`` present on a table, broadest layer first.

    Vendored from ``packages/ratch/features/topic_tree.py`` (``topic_layer_columns``)
    because the backend may not import the pipeline package (§4.4); kept
    byte-equivalent in behavior so the search topic filter can't drift from the
    layers the topic tree/atlas were built on. The ``isdigit`` guard skips
    non-layer columns that merely share the prefix.
    """
    return sorted(
        (c for c in schema_names if c.startswith(_TOPIC_LAYER_PREFIX) and c.removeprefix(_TOPIC_LAYER_PREFIX).isdigit()),
        key=lambda c: int(c.removeprefix(_TOPIC_LAYER_PREFIX)),
    )


def extract_filters(params: Mapping[str, str], filterable: Sequence[str]) -> dict[str, str]:
    """Pick the descriptor-declared filter params out of a query-string/form mapping.

    Only names in ``filterable`` are honored (everything else in the request is
    someone else's parameter), and empty values read as "filter not set".
    """
    out: dict[str, str] = {}
    for field in filterable:
        value = params.get(field)
        if value:
            out[field] = value
    return out


def build_where_clause(
    *,
    filters: Mapping[str, str],
    filterable: Sequence[str],
    topic_columns: Sequence[str] = (),
    raw: str | None = None,
) -> str | None:
    """Compose the SQL WHERE clause for the descriptor-declared metadata filters.

    Each filterable field compiles to an equality clause; the ``topic`` field
    keeps the old topic-layer expansion (OR-equality over ``topic_columns``,
    dropped when the table has no layers). ``raw`` is a user-typed SQL
    expression ANDed in verbatim (wrapped in parens) — intentionally *not*
    quoted, since it is meant to be SQL, not a value.
    """
    clauses: list[str] = []
    for field in filterable:
        value = filters.get(field)
        if not value:
            continue
        if field == TOPIC_FILTER:
            if topic_columns:
                ors = " OR ".join(eq(col, value) for col in topic_columns)
                clauses.append(f"({ors})")
            continue
        clauses.append(eq(field, value))
    if raw and raw.strip():
        clauses.append(f"({raw})")
    return " AND ".join(clauses) if clauses else None
