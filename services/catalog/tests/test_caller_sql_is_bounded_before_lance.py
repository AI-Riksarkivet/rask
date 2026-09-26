"""`refuse_an_unbounded_boolean_chain` counts a fragment's AND/OR structure, not its text.

The crash it prevents is measured in `tests/integration/test_a_long_boolean_chain_is_refused_not_a_crash.py`;
this file pins the counting rule, which decides what a caller may still send.
"""

from __future__ import annotations

from typing import cast

import pytest
from lance_namespace import InvalidInputError

from catalog.services.dataplane import MAX_CALLER_SQL_CONNECTIVES, refuse_an_unbounded_boolean_chain


def _chain(terms: int) -> str:
    return " OR ".join(f"id = {i}" for i in range(terms))


def test_the_bound_is_inclusive() -> None:
    refuse_an_unbounded_boolean_chain(_chain(MAX_CALLER_SQL_CONNECTIVES + 1), field="filter")

    with pytest.raises(InvalidInputError, match="joins 1001 conditions with AND/OR"):
        refuse_an_unbounded_boolean_chain(_chain(MAX_CALLER_SQL_CONNECTIVES + 2), field="filter")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("v = '" + " or " * 2_000 + "'", id="inside-a-string-literal"),
        pytest.param("v = 'it''s" + " and " * 2_000 + "'", id="inside-a-literal-with-an-escaped-quote"),
        pytest.param('"' + " or " * 2_000 + '" = 1', id="inside-a-quoted-identifier"),
        pytest.param("color + floor + orders = " + " + ".join(["color"] * 2_000), id="inside-longer-names"),
        pytest.param("id IN (" + ",".join(str(i) for i in range(200_000)) + ")", id="an-in-list"),
    ],
)
def test_text_that_is_not_structure_is_not_counted(text: str) -> None:
    refuse_an_unbounded_boolean_chain(text, field="filter")


def test_no_fragment_is_nothing_to_bound() -> None:
    refuse_an_unbounded_boolean_chain(None, field="filter")


def test_a_fragment_that_is_not_text_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="filter must be a SQL string"):
        refuse_an_unbounded_boolean_chain(cast("str", 42), field="filter")
