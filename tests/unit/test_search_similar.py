""" "More like this" — k-NN from a row the caller already has (#42 step 3).

The bulk surface can label a selection; what it could not do is BUILD one from an example. Search
answers a query, and the query for "another four hundred pages that look like this one" is the page
itself — the embedding is already there, and nothing could reach it.

This is the pure half: turning a row key into the seed's own vector, and turning that into
neighbours. Five properties carry it, and a wrong implementation looks right for every one:

* The SEED is excluded from its own neighbours. Its distance is 0, so it always ranks first —
  "more like this" whose best answer is the thing you clicked is a surface nobody uses twice.
* A key must have the same ARITY as the identity. A two-part key against three key fields is a
  prefix, and a prefix matches several rows — silently seeding from whichever came back first.
* A corpus with no vector binding REFUSES. Returning `[]` reads as "nothing here is similar",
  which is a claim about the corpus rather than about the corpus's configuration.
* A key that matches no row REFUSES, rather than searching from a null vector — which returns the
  corpus's nearest to the origin, a plausible-looking list of entirely unrelated rows.
* The seed's own vector is read from the binding's COLUMN, not guessed.
"""

from __future__ import annotations

from typing import Any

import pytest

from search.services.similar import (
    SimilarSpec,
    drop_seed,
    key_predicate,
    seed_vector,
)
from service_kit.exceptions import NotFoundError, ValidationError
from service_kit.lancekit.descriptor import Declared


KEY_FIELDS = ["doc_id", "speech_id", "chunk_id"]
#: `key_predicate` renders through `lancekit.keys`, which needs the whole identity (which field
#: is the doc key, and therefore which segments are integer sub-keys) — not just the field names.
DECLARED = Declared.model_validate({"identity": {"key_fields": KEY_FIELDS, "doc_key": "doc_id"}})


# --------------------------------------------------------------------------------------------------
# The key is an identity, not a prefix
# --------------------------------------------------------------------------------------------------


def test_a_key_compiles_to_an_equality_over_every_key_field() -> None:
    where = key_predicate(DECLARED, "d1/2/3")

    assert "doc_id" in where
    assert "speech_id" in where
    assert "chunk_id" in where
    assert where.count(" AND ") == 2, "the parts must be ANDed, not ORed or concatenated"


def test_a_short_key_is_REFUSED_not_treated_as_a_prefix() -> None:
    """`d1/2` against three key fields matches every chunk of that speech. Seeding from "whichever
    row came back first" is a different answer every time the table compacts."""
    with pytest.raises(ValidationError) as exc:
        key_predicate(DECLARED, "d1/2")

    assert "3" in str(exc.value), "the refusal must say how many parts were expected"


def test_a_long_key_is_refused_too() -> None:
    with pytest.raises(ValidationError):
        key_predicate(DECLARED, "d1/2/3/4")


def test_a_quote_in_a_key_cannot_break_out_of_the_predicate() -> None:
    """The key is user input reaching a SQL string. `eq` is the injection boundary; this pins that
    it is actually the thing being used."""
    where = key_predicate(DECLARED, "d'1/2/3")

    assert "''" in where or "\\'" in where, f"the quote was not escaped: {where}"


def test_an_empty_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        key_predicate(DECLARED, "")


# --------------------------------------------------------------------------------------------------
# The seed's own vector
# --------------------------------------------------------------------------------------------------


class _Table:
    """A LanceDB table stand-in: `search().where().limit().to_list()` and a schema."""

    def __init__(self, rows: list[dict[str, Any]], columns: list[str]) -> None:
        self.rows = rows
        self.schema = type("S", (), {"names": columns})()
        self.seen_where: str | None = None

    def search(self, *_a: Any, **_k: Any) -> _Table:
        return self

    def where(self, clause: str, **_k: Any) -> _Table:
        self.seen_where = clause
        return self

    def select(self, *_a: Any, **_k: Any) -> _Table:
        return self

    def limit(self, *_a: Any, **_k: Any) -> _Table:
        return self

    def to_list(self) -> list[dict[str, Any]]:
        return self.rows


def test_the_seed_vector_comes_from_the_declared_column() -> None:
    table = _Table([{"doc_id": "d1", "embedding": [0.1, 0.2, 0.3]}], ["doc_id", "embedding"])

    vec = seed_vector(table, where="doc_id = 'd1'", column="embedding")

    assert vec == [0.1, 0.2, 0.3]


def test_a_key_matching_no_row_is_a_404_not_a_null_vector() -> None:
    """Searching from `None` returns the corpus's nearest to the origin — a plausible-looking list
    of entirely unrelated rows, with nothing to say it is wrong."""
    table = _Table([], ["doc_id", "embedding"])

    with pytest.raises(NotFoundError):
        seed_vector(table, where="doc_id = 'nope'", column="embedding")


def test_a_row_whose_vector_is_null_is_refused() -> None:
    """A row can exist and not have been embedded yet — the pipeline runs in stages. That is not
    the same as the row not existing, and it is not a reason to search from nothing."""
    table = _Table([{"doc_id": "d1", "embedding": None}], ["doc_id", "embedding"])

    with pytest.raises(NotFoundError):
        seed_vector(table, where="doc_id = 'd1'", column="embedding")


def test_a_missing_embedding_column_is_refused_rather_than_returning_nothing() -> None:
    """A stale descriptor declares a column the table does not carry. `vector_search` degrades to
    `[]` there on purpose — for FUSION, where the other legs still rank. Here there is no other
    leg, so `[]` would be an empty answer to a question that was never asked."""
    table = _Table([{"doc_id": "d1"}], ["doc_id"])

    with pytest.raises(ValidationError):
        seed_vector(table, where="doc_id = 'd1'", column="embedding")


# --------------------------------------------------------------------------------------------------
# The seed is not its own neighbour
# --------------------------------------------------------------------------------------------------


def test_the_seed_is_dropped_from_its_own_neighbours() -> None:
    """Distance 0 always ranks first. A "more like this" whose top hit is the thing you clicked
    wastes a row of the grid and reads as broken."""
    hits = [
        {"doc_id": "d1", "speech_id": 2, "chunk_id": 3, "_distance": 0.0},
        {"doc_id": "d1", "speech_id": 2, "chunk_id": 9, "_distance": 0.11},
    ]

    kept = drop_seed(hits, "d1/2/3", KEY_FIELDS)

    assert [h["chunk_id"] for h in kept] == [9]


def test_dropping_the_seed_compares_as_STRINGS() -> None:
    """Key fields are mixed types — a string doc id beside integer speech/chunk ids. Lance returns
    the integers as ints and the key path is text, so `3 != "3"` would silently never match and the
    seed would survive every time."""
    hits = [{"doc_id": "d1", "speech_id": 2, "chunk_id": 3, "_distance": 0.0}]

    assert drop_seed(hits, "d1/2/3", KEY_FIELDS) == []


def test_only_the_seed_is_dropped_not_every_row_sharing_a_document() -> None:
    hits = [
        {"doc_id": "d1", "speech_id": 2, "chunk_id": 3, "_distance": 0.0},
        {"doc_id": "d1", "speech_id": 2, "chunk_id": 4, "_distance": 0.2},
        {"doc_id": "d1", "speech_id": 5, "chunk_id": 3, "_distance": 0.3},
    ]

    kept = drop_seed(hits, "d1/2/3", KEY_FIELDS)

    assert len(kept) == 2


# --------------------------------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------------------------------


def test_the_spec_defaults_n_to_something_a_grid_can_show() -> None:
    spec = SimilarSpec(key="d1/2/3")

    assert 1 <= spec.n <= 200


def test_n_is_capped_so_one_call_cannot_ask_for_the_corpus() -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic's own error type is not the point
        SimilarSpec(key="d1/2/3", n=100_000)


def test_the_spec_asks_for_ONE_MORE_than_requested() -> None:
    """The seed occupies a slot in the k-NN result and is then dropped, so asking for exactly `n`
    returns `n-1`. Off-by-one here is invisible: the grid just looks one short."""
    spec = SimilarSpec(key="d1/2/3", n=24)

    assert spec.fetch_n == 25
