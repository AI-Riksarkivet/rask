"""Searching several corpora at once, fused by reciprocal rank.

`?corpus=a&corpus=b` fans out. Its own parameter rather than a repeated `dataset`, because repeating
that one would change its type from every existing caller's point of view, and they all send exactly
one.

The behaviours worth pinning are the ones a fan-out gets wrong quietly: a corpus that refuses the
request must not take the others down, each corpus must be filtered by its OWN declared fields, and
the result must be RANK-fused rather than score-sorted.
"""

from __future__ import annotations

from typing import Any

import pytest

from search.api.v1 import router as router_mod
from search.services.fuse import RRF_SCORE
from service_kit.exceptions import ValidationError


class _Declared:
    def __init__(self, keys: list[str]) -> None:
        self.identity = type("I", (), {"key_fields": keys})()
        self.search = type("S", (), {"filterable": []})()

    def search_named(self, _name: str | None) -> Any:
        return self.search


class _Handle:
    def __init__(self, dataset_id: str, keys: list[str]) -> None:
        self.descriptor = type("D", (), {"id": dataset_id, "declared": _Declared(keys)})()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    results: dict[str, list[dict[str, Any]]],
    refuse: set[str] | None = None,
) -> list[str]:
    """Fake the two seams `_fused_search` reaches through, recording which corpora were searched."""
    searched: list[str] = []
    refuse = refuse or set()

    def dataset_handle(_state: Any, dataset_id: str | None = None) -> _Handle:
        if dataset_id in refuse:
            raise ValidationError(f"corpus {dataset_id} has no search bindings")
        return _Handle(str(dataset_id), ["doc_id"])

    def cached(_state: Any, handle: _Handle, *_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        searched.append(handle.descriptor.id)
        return results.get(handle.descriptor.id, [])

    monkeypatch.setattr(router_mod, "dataset_handle", dataset_handle)
    monkeypatch.setattr(router_mod, "_cached_search", cached)
    return searched


class _Request:
    query_params: dict[str, str] = {}


def _spec(n: int = 10) -> Any:
    return type("Spec", (), {"q": "vasa", "q_vec": "", "n": n, "table": None, "dataset": None})()


def hit(dataset: str, doc: str, score: float = 0.0) -> dict[str, Any]:
    return {"_dataset": dataset, "_table": "default", "doc_id": doc, "_score": score}


def fan(corpora: list[str], **kw: Any) -> list[dict[str, Any]]:
    # Structural stand-ins: `_fused_search` reaches state and request only through the two seams
    # faked above (`dataset_handle`, `extract_filters`), so building a real AppState or Starlette
    # Request would add setup that proves nothing about the fan-out.
    state: Any = None
    request: Any = _Request()
    return router_mod._fused_search(state, corpora, request, _spec(kw.pop("n", 10)), None, None)


def test_every_named_corpus_is_searched(monkeypatch: pytest.MonkeyPatch) -> None:
    searched = _install(monkeypatch, results={"a": [hit("a", "x")], "b": [hit("b", "y")]})

    fan(["a", "b"])

    assert searched == ["a", "b"]


def test_the_result_is_RANK_fused_not_score_sorted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole reason the fan-out does not simply concatenate: `_score` from one corpus and
    `_score` from another are not comparable, so a merged sort by them means nothing."""
    _install(
        monkeypatch,
        results={"a": [hit("a", "low", score=0.01)], "b": [hit("b", "high", score=999.0)]},
    )

    fused = fan(["a", "b"])

    assert all(RRF_SCORE in h for h in fused)
    # Both ranked first in their own list, so they TIE — a score sort would have put `high` first.
    assert {h[RRF_SCORE] for h in fused} == {fused[0][RRF_SCORE]}


def test_a_REFUSING_corpus_is_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """One misconfigured corpus must not take the others down. A fan-out that 400s because the third
    of five corpora declares no search bindings is unusable in exactly the estate it exists for."""
    searched = _install(monkeypatch, results={"a": [hit("a", "x")], "c": [hit("c", "z")]}, refuse={"b"})

    fused = fan(["a", "b", "c"])

    assert searched == ["a", "c"]
    assert {h["_dataset"] for h in fused} == {"a", "c"}


def test_a_corpus_is_searched_ONCE_even_if_named_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise a duplicated param would double that corpus's rank contribution and quietly bias the
    fusion toward it."""
    searched = _install(monkeypatch, results={"a": [hit("a", "x")]})

    fan(["a", "a"])

    assert searched == ["a"]


def test_provenance_survives_the_fan_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without it the frontend cannot resolve each hit's own view — which is the entire point of the
    fused list."""
    _install(monkeypatch, results={"a": [hit("a", "x")], "b": [hit("b", "y")]})

    fused = fan(["a", "b"])

    assert {h["_dataset"] for h in fused} == {"a", "b"}
    assert all("_table" in h for h in fused)


def test_the_LIMIT_is_the_spec_n(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        results={"a": [hit("a", f"x{i}") for i in range(5)], "b": [hit("b", f"y{i}") for i in range(5)]},
    )

    assert len(fan(["a", "b"], n=3)) == 3


def test_all_corpora_refusing_yields_an_empty_list_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty result is the honest answer to "nothing here could be searched" — and it is what the
    single-corpus path already returns for an unmatched query."""
    _install(monkeypatch, results={}, refuse={"a", "b"})

    assert fan(["a", "b"]) == []
