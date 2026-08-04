"""Ranking results from several corpora.

The reason this exists rather than a merge-by-score: BM25 is normalised per index and vector
distances depend on the space, so `_score` from corpus A and `_score` from corpus B are not
comparable. Sorting a merged list by them produces an ordering that looks authoritative and means
nothing — and nothing in the results would show it.

RRF throws the scores away and fuses by RANK, which is the only signal that survives the comparison.
"""

from __future__ import annotations

from typing import Any

from search.services.fuse import DEFAULT_K, RRF_SCORE, reciprocal_rank_fusion


def hit(dataset: str, doc: str, score: float = 0.0, table: str = "default") -> dict[str, Any]:
    return {"_dataset": dataset, "_table": table, "doc_id": doc, "_score": score}


KEYS = ["doc_id"]


def fuse(*lists: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    return reciprocal_rank_fusion(lists, key_fields=KEYS, **kw)


def test_the_engine_SCORE_is_ignored_entirely() -> None:
    """The whole point. `b` carries a wildly higher score but is ranked second in its own list, so it
    must not outrank a document that both TABLES of the corpus put first."""
    pages = [hit("A", "shared", score=0.1, table="pages"), hit("A", "b", score=999.0, table="pages")]
    lines = [hit("A", "shared", score=0.1, table="lines")]

    fused = fuse(pages, lines)

    assert fused[0]["doc_id"] == "shared"


def test_agreement_across_TABLES_of_one_corpus_COMPOUNDS() -> None:
    """The property RRF is chosen for, on the axis where agreement is real.

    `pages` and `lines` index the same material at different granularities, so a document both rank
    highly is genuine agreement — and it must beat one only a single table loves.
    """
    pages = [hit("A", "agreed", table="pages"), hit("A", "solo", table="pages")]
    lines = [hit("A", "agreed", table="lines")]

    fused = fuse(pages, lines)

    assert [h["doc_id"] for h in fused] == ["agreed", "solo"]


def test_the_fused_score_is_the_published_formula() -> None:
    """Pinned so nobody "improves" the constant without noticing it is the published one."""
    once = fuse([hit("A", "x")])
    assert once[0][RRF_SCORE] == 1.0 / (DEFAULT_K + 1)

    # Agreed at rank 0 in two tables of one corpus: the two terms add.
    twice = fuse([hit("A", "x", table="pages")], [hit("A", "x", table="lines")])
    assert twice[0][RRF_SCORE] == 2.0 / (DEFAULT_K + 1)


def test_the_SAME_key_in_two_corpora_stays_two_documents() -> None:
    """They are different rows about different things that happen to share an id. Merging them would
    silently drop one and attribute the other to the wrong corpus."""
    fused = fuse([hit("A", "x")], [hit("B", "x")])

    assert len(fused) == 2
    assert {h["_dataset"] for h in fused} == {"A", "B"}


def test_the_fused_score_lands_under_its_OWN_name() -> None:
    """A consumer must be able to tell a fused ordering from a single-corpus one: only the former is
    comparable across corpora, and only `_score` carries the engine's own relevance number."""
    fused = fuse([hit("A", "x", score=3.2)])

    assert RRF_SCORE in fused[0]
    assert fused[0]["_score"] == 3.2, "the engine's own score survives untouched"


def test_provenance_survives_the_fusion() -> None:
    """Without it a fused hit cannot resolve its own display fields, media kind or capabilities —
    which is the entire reason hits are stamped."""
    fused = fuse([hit("A", "x")], [hit("B", "y")])

    assert all("_dataset" in h and "_table" in h for h in fused)


def test_a_LIMIT_cuts_after_fusing_not_before() -> None:
    """Cutting each list first would drop a document that ranks mid-list everywhere but wins overall
    — the exact case fusion exists to find."""
    pages = [hit("A", "top", table="pages"), hit("A", "mid", table="pages")]
    lines = [hit("A", "other", table="lines"), hit("A", "mid", table="lines")]

    fused = fuse(pages, lines, limit=1)

    assert len(fused) == 1
    assert fused[0]["doc_id"] == "mid", "agreed-on-second beats first-in-one-table"


def test_the_order_is_DETERMINISTIC_for_tied_documents() -> None:
    """An unstable order across identical requests makes pagination lie, and it only shows up under
    load."""
    a = [hit("A", "x"), hit("A", "y"), hit("A", "z")]

    assert [h["doc_id"] for h in fuse(a)] == [h["doc_id"] for h in fuse(a)]


def test_an_EMPTY_corpus_contributes_nothing_and_breaks_nothing() -> None:
    assert [h["doc_id"] for h in fuse([hit("A", "x")], [])] == ["x"]
    assert fuse([], []) == []


def test_one_corpus_fuses_to_its_own_order() -> None:
    """A single-corpus search must survive the same path unchanged — otherwise the fan-out would be a
    different product for one corpus than for two."""
    a = [hit("A", "first"), hit("A", "second"), hit("A", "third")]

    assert [h["doc_id"] for h in fuse(a)] == ["first", "second", "third"]
