"""`or_` must PARENTHESISE its legs (#60).

The batch rows-by-key read ORs together conjunctions built by `and_` — a row key is
`doc = 'x' AND chunk = 1`. Joined bare, the SQL operator precedence (`AND` binds tighter than
`OR`) regroups the predicate, and a filter that silently returns the WRONG rows is worse than one
that errors, because wrong rows look like data.
"""

from __future__ import annotations

from service_kit.lancekit.predicate import and_, eq, or_


def test_each_leg_is_parenthesised() -> None:
    assert or_("a = 1", "b = 2") == "(a = 1) OR (b = 2)"


def test_ORed_conjunctions_keep_their_grouping() -> None:
    """The real shape, and the reason the parens exist. Without them this renders as
    `doc = 'a' AND chunk = 1 OR doc = 'b' AND chunk = 2`, whose grouping depends entirely on
    precedence rather than on what the caller meant."""
    left = and_(eq("doc_id", "a"), eq("chunk_id", 1))
    right = and_(eq("doc_id", "b"), eq("chunk_id", 2))

    joined = or_(left, right)

    assert joined == f"({left}) OR ({right})"
    assert joined.count("(") >= 2


def test_a_single_leg_is_still_wrapped() -> None:
    """A one-key batch is the common case for a small queue page. Wrapping is harmless here and
    keeps the output shape uniform, so a caller cannot come to depend on an unwrapped single."""
    assert or_("a = 1") == "(a = 1)"


def test_empty_clauses_are_dropped_like_and_() -> None:
    """Mirrors `and_`, so a caller can pass an optional leg unconditionally without composing a
    predicate with a dangling `OR ()`."""
    assert or_("a = 1", "", "b = 2") == "(a = 1) OR (b = 2)"


def test_no_clauses_yields_an_EMPTY_string_not_a_match_everything() -> None:
    """The dangerous direction. `or_()` of nothing must not render something a Lance filter reads
    as TRUE — an empty batch has to be caught by the caller (the endpoint returns `[]` before
    building a predicate), and an empty string cannot be mistaken for a passing clause.
    """
    assert or_() == ""
