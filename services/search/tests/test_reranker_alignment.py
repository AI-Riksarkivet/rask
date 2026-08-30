"""A reranker's scores must line up with the candidates it was given, one per candidate.

open_python-audit `VS-14` (E4, med). `rerank` promised "one relevance score per candidate, in input
order" and delivered it only when the server returned a DENSE, full-length result list. It built the
answer with `sorted((item.index, score) for item in results)` and returned the scores stripped of
their indices — so a SHORT list (the server scored fewer than it was asked) or a SPARSE one (it
skipped an index) produced a list whose positions no longer map to the candidates: candidate *i* is
handed candidate *j*'s score, and the caller (`rerank.py`, `zip(scores, head, strict=False)`) drops
the tail without a word.

WHY IT MATTERS beyond a wrong number: the whole point of the rerank pass is ordering, so a
misaligned score reorders the WRONG documents to the top — a silent relevance regression that no
error surfaces. `strict=False` is what lets the length mismatch pass; the index-keyed rebuild is
what makes the SPARSE case correct rather than merely non-crashing.

A DECLINED index sinks, it does not steal: a candidate the server returned no score for gets a
floor score (ranks last, honestly) rather than inheriting a neighbour's — the reranker declining to
score a document is not evidence that document is relevant.
"""

from __future__ import annotations

from typing import Any

import pytest

from search.services.encoders.base import RerankResponse, RerankResult
from search.services.encoders.reranker import VLLMReranker


class _FakeTransport:
    """Stands in for VLLMTransport: returns a scripted RerankResponse, records the request."""

    def __init__(self, results: list[RerankResult]) -> None:
        self._results = results
        self.sent: dict[str, Any] | None = None

    def post(self, _path: str, body: dict[str, Any], *, into: type[Any]) -> Any:
        self.sent = body
        assert into is RerankResponse
        return RerankResponse(results=self._results)

    def close(self) -> None: ...


def _reranker(results: list[RerankResult], monkeypatch: pytest.MonkeyPatch) -> VLLMReranker:
    """Build a reranker whose transport is the fake, without touching the real VLLMTransport ctor
    (which opens an httpx pool). `setattr` through monkeypatch keeps the swap out of ty's view — the
    attribute is a genuine instance field, the fake satisfies the one method `rerank` calls."""
    monkeypatch.setattr("search.services.encoders.reranker.VLLMTransport", lambda *_a, **_k: _FakeTransport(results))
    return VLLMReranker("http://rerank.invalid", model="m", instruction="i")


def test_a_full_unordered_reply_still_maps_each_score_to_its_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case that always worked, kept: an out-of-order full reply lands each score on its own row."""
    r = _reranker(
        [RerankResult(index=2, relevance_score=0.9), RerankResult(index=0, relevance_score=0.1), RerankResult(index=1, relevance_score=0.5)], monkeypatch
    )
    assert r.rerank("q", ["a", "b", "c"]) == [0.1, 0.5, 0.9]


def test_a_sparse_reply_does_not_shift_scores_onto_the_wrong_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server scored candidates 0 and 2 and skipped 1. Candidate 2 must keep 0.9 — not inherit
    it at position 1 the way the sorted-and-stripped list did."""
    scores = _reranker([RerankResult(index=0, relevance_score=0.1), RerankResult(index=2, relevance_score=0.9)], monkeypatch).rerank("q", ["a", "b", "c"])
    assert len(scores) == 3, "the reply was short and the result silently lost a candidate"
    assert scores[0] == 0.1
    assert scores[2] == 0.9, "candidate 2's score landed on candidate 1 — the misalignment VS-14 names"


def test_a_declined_candidate_sinks_rather_than_stealing_a_neighbours_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """The skipped index gets a floor score, so it ranks LAST, not wherever a shifted neighbour put it."""
    scores = _reranker([RerankResult(index=0, relevance_score=0.1), RerankResult(index=2, relevance_score=0.9)], monkeypatch).rerank("q", ["a", "b", "c"])
    assert scores[1] < min(0.1, 0.9), "an unscored candidate did not sink below the scored ones"


def test_the_result_length_always_equals_the_candidate_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract the caller relies on: N candidates in, N scores out — so its zip needs no
    `strict=False` to hide a length mismatch."""
    for reply in ([], [RerankResult(index=1, relevance_score=0.5)]):
        assert len(_reranker(reply, monkeypatch).rerank("q", ["a", "b", "c", "d"])) == 4


def test_an_out_of_range_index_is_refused_not_silently_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server bug that returns index 9 for 3 candidates must not corrupt the mapping silently."""
    with pytest.raises(ValueError, match="out of range"):
        _reranker([RerankResult(index=9, relevance_score=0.9)], monkeypatch).rerank("q", ["a", "b", "c"])


def test_no_candidates_is_still_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _reranker([], monkeypatch).rerank("q", []) == []
