"""The search service's routing decisions had no tests, and they decide what work runs.

`services/search/services/service.py` sat at 16% coverage — 140 of 179 statements — and the
uncovered part includes the two functions that decide, per request, WHICH retrieval leg executes and
whether a query embedding is computed at all. Embedding is the expensive half: it loads a model and
runs inference before any Lance read happens, so a mode that wrongly reports it needs one pays that
cost on every keyword search, and a mode that wrongly reports it does not gets a vector leg with no
vector.

Neither function touches Lance or lancedb — they route on the declared target and the mode string —
so the gap was never about needing a dataset.

Boundaries per `writing-python` references/testing.md § "Test boundary conditions (T5)": the empty
mode, a declared key vs an undeclared one, the `_fts` suffix against a key that merely CONTAINS it,
and the composite modes that always embed regardless of what is declared.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from search.services.service import _mode_needs_query_vector
from search.services.spec import SearchMode
from search.services.target import SearchTarget


class _Fake:
    """Structural double — `_mode_needs_query_vector` calls exactly one method on its target."""

    def __init__(self, declared: set[str]) -> None:
        self._declared = declared

    def binding(self, mode: str) -> object | None:
        return object() if mode in self._declared else None


def _Target(declared: set[str]) -> SearchTarget:
    """The double, cast to the real type — the estate's fake-by-shape idiom (`testing-python`:
    `cast(JobSubmissionClient, fake)`), which keeps the signature honest without a suppression."""
    return cast("SearchTarget", _Fake(declared))


class TestCompositeModesAlwaysEmbed:
    """hybrid and all fuse a vector leg with BM25, so they need the embedding no matter what the
    dataset declares — and asking the target would give the wrong answer for exactly these two."""

    @pytest.mark.parametrize("mode", [SearchMode.HYBRID.value, SearchMode.ALL.value])
    def test_it_needs_a_vector_even_when_nothing_is_declared(self, mode: str) -> None:
        assert _mode_needs_query_vector(_Target(set()), mode) is True


class TestKeywordModesNeverEmbed:
    def test_plain_fts_does_not(self) -> None:
        """BM25 over text. Embedding here would load a model to rank lexically."""
        assert _mode_needs_query_vector(_Target(set()), SearchMode.FTS.value) is False

    def test_the_two_routers_DISAGREE_if_a_dataset_declares_a_vector_named_fts(self) -> None:
        """A latent trap, pinned rather than fixed because fixing it silently is the worse move.

        `_mode_needs_query_vector` has no special case for plain `fts`: it falls through to
        `target.binding("fts")`. `_dispatch` DOES have one — `fts` is in `_COMPOSITE_HANDLERS`, so it
        routes to the BM25 handler. A dataset declaring a vector space under the key `fts` therefore
        makes the two disagree: an embedding is computed (model load + inference) and then thrown
        away, because the leg that runs never looks at it.

        No shipped dataset declares that key, which is why it has never fired, and the docstring says
        plain fts does not embed — so the code and its own description already differ for this input.
        The reserved composite names (`fts`, `hybrid`, `all`) are the collision surface; pinning it
        means a dataset that ever declares one fails a test rather than paying for a wasted embed."""
        target = _Target({SearchMode.FTS.value})

        assert _mode_needs_query_vector(target, SearchMode.FTS.value) is True, (
            "if this is now False, the collision was fixed — good, and this test should assert that"
        )

    def test_a_key_fts_leg_does_not(self) -> None:
        """`<key>_fts` is BM25 over that binding's caption SOURCE, not its embedding."""
        assert _mode_needs_query_vector(_Target({"scene"}), SearchMode.SCENE_FTS.value) is False

    def test_the_suffix_is_matched_at_the_END_not_anywhere(self) -> None:
        """A declared key that merely CONTAINS 'fts' is a vector space, not a BM25 leg. Matching the
        substring would silently stop embedding for it and return an empty vector leg."""
        assert _mode_needs_query_vector(_Target({"fts_caption"}), "fts_caption") is True


class TestDeclaredVectorSpaces:
    def test_a_declared_key_needs_its_embedding(self) -> None:
        assert _mode_needs_query_vector(_Target({"semantic"}), SearchMode.SEMANTIC.value) is True

    def test_an_UNDECLARED_key_does_not(self) -> None:
        """Every embedding space is optional. An absent one is empty, not an error — so there is
        nothing to embed FOR, and computing one would be pure waste before an empty read."""
        assert _mode_needs_query_vector(_Target(set()), SearchMode.SEMANTIC.value) is False

    def test_the_empty_mode_needs_nothing(self) -> None:
        assert _mode_needs_query_vector(_Target(set()), "") is False


class TestDispatchRoutesToOneLeg:
    """`_dispatch` picks the handler. Getting it wrong runs a different retrieval than the caller
    asked for and returns plausible rows, which is worse than an error."""

    def _ctx(self, mode: str) -> Any:
        """`_dispatch` reads exactly one field: `ctx.spec.mode`."""

        class _Spec:
            def __init__(self, mode: str) -> None:
                self.mode = mode

        class _Ctx:
            def __init__(self, mode: str) -> None:
                self.spec = _Spec(mode)

        return _Ctx(mode)

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            (SearchMode.FTS.value, "_search_fts"),
            (SearchMode.HYBRID.value, "_search_hybrid"),
            (SearchMode.ALL.value, "_search_all"),
        ],
    )
    def test_a_composite_mode_reaches_its_own_handler(self, monkeypatch: pytest.MonkeyPatch, mode: str, expected: str) -> None:
        from search.services import service

        seen: list[str] = []
        for name in ("_search_fts", "_search_hybrid", "_search_all", "_search_vector_mode", "_search_vector_fts_mode"):
            monkeypatch.setattr(service, name, (lambda n: lambda _ctx: seen.append(n) or [])(name))
        # the map is built at import time from the ORIGINAL functions, so rebuild it against the fakes
        monkeypatch.setattr(
            service,
            "_COMPOSITE_HANDLERS",
            {
                SearchMode.FTS.value: service._search_fts,
                SearchMode.HYBRID.value: service._search_hybrid,
                SearchMode.ALL.value: service._search_all,
            },
        )

        service._dispatch(self._ctx(mode))

        assert seen == [expected]

    def test_a_key_fts_mode_reaches_the_bm25_leg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from search.services import service

        seen: list[str] = []
        monkeypatch.setattr(service, "_search_vector_fts_mode", lambda _c: seen.append("fts_leg") or [])
        monkeypatch.setattr(service, "_search_vector_mode", lambda _c: seen.append("vector_leg") or [])

        service._dispatch(self._ctx(SearchMode.SCENE_FTS.value))

        assert seen == ["fts_leg"]

    def test_an_undeclared_mode_falls_through_to_the_vector_leg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Graceful by design: an undeclared space yields no results rather than raising, because
        every embedding space is optional and the frontend only offers declared ones."""
        from search.services import service

        seen: list[str] = []
        monkeypatch.setattr(service, "_search_vector_mode", lambda _c: seen.append("vector_leg") or [])

        service._dispatch(self._ctx("nothing-declares-this"))

        assert seen == ["vector_leg"]
