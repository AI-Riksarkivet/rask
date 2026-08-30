"""The search spec accepted an unbounded query string, and its cache missed on every variation.

open_fastapi-audit, the two halves of the `/search` finding that the rate limiter does not address.
A limiter bounds how OFTEN the work happens; these bound how EXPENSIVE one unit of it is, and whether
the second identical request pays again.

UNBOUNDED INPUT. `q` and `q_vec` were bare `str = ""`. Both reach a GPU embedding forward pass, and
`q` also reaches the FTS engine — so a multi-megabyte query string is one request that costs orders
of magnitude more than a normal one, under whatever the rate limit permits. `n`, `rerank_n`,
`fuzziness` and `weight` are all bounded already; the two free-text fields, which are the ones that
drive the model, were not.

REJECT rather than clamp, and that is a deliberate departure from the neighbours. `_clamp_n` exists
because the frontend's "load more" legitimately raises `n` past 100 and should not error. There is no
equivalent story for a 10 MB query: truncating it silently would return results for a query the
caller did not ask, which is worse than a 422 naming the limit.

UNBOUNDED CACHE CARDINALITY. `cache.md`: cache "only for keys with bounded cardinality and high
hit-rate". A raw free-text `q` has neither — `q="cat"`, `q="cat "` and `q="Cat"` are three keys for
one question, so a caller varying whitespace or case never hits and every request is a fresh GPU
pass. Normalizing the key is what makes the cache a cache; it does NOT change what the search reads,
only which requests share an entry.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from search.services.result_cache import query_hash
from search.services.spec import MAX_QUERY_CHARS, SearchSpec


class TestTheQueryStringIsBounded:
    def test_an_oversized_q_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            SearchSpec(q="x" * (MAX_QUERY_CHARS + 1))

    def test_an_oversized_q_vec_is_refused(self) -> None:
        """`q_vec` is the vector leg's own text and reaches the same encoder."""
        with pytest.raises(ValidationError):
            SearchSpec(q_vec="x" * (MAX_QUERY_CHARS + 1))

    def test_a_normal_query_is_untouched(self) -> None:
        """The guard against a bound tight enough to reject real queries."""
        spec = SearchSpec(q="what did the minister say about the harbour in 1897")
        assert spec.q.startswith("what did")

    def test_a_query_AT_the_limit_is_accepted(self) -> None:
        assert len(SearchSpec(q="x" * MAX_QUERY_CHARS).q) == MAX_QUERY_CHARS


class TestTheCacheKeyIsNormalized:
    """A cache whose key varies with noise the search does not care about is a free miss generator."""

    def _hash(self, q: str) -> str:
        return query_hash(SearchSpec(q=q), None, None)

    def test_surrounding_whitespace_shares_an_entry(self) -> None:
        assert self._hash("harbour") == self._hash("  harbour  ")

    def test_internal_whitespace_runs_share_an_entry(self) -> None:
        assert self._hash("the harbour") == self._hash("the    harbour")

    def test_case_shares_an_entry(self) -> None:
        """FTS and the encoders are case-insensitive for this purpose; the key should be too."""
        assert self._hash("Harbour") == self._hash("harbour")

    def test_GENUINELY_different_queries_do_not_collide(self) -> None:
        """The bound in the other direction, and the more important one: normalizing must not serve
        one query's results for another."""
        assert self._hash("harbour") != self._hash("harbor")
        assert self._hash("the harbour") != self._hash("harbour the")

    def test_normalization_does_not_change_what_is_SEARCHED(self) -> None:
        """Only the cache KEY folds case and internal spacing. The spec still carries them.

        Surrounding whitespace is a separate, PRE-EXISTING concern: `_strip` on the model has always
        trimmed it, so the handlers' empty-input short-circuit and `q_vec`'s fall-back-to-`q`
        contract hold. What the key adds is case and internal runs — and those must NOT be folded
        into `spec.q`, because that text reaches the FTS engine where the analyzer can care.
        """
        spec = SearchSpec(q="  The   Harbour  ")

        assert spec.q == "The   Harbour", "the model stopped stripping, or started over-normalizing"
        assert "The" in spec.q, "case was folded into what is searched, not just into the key"
        assert "   " in spec.q, "internal spacing was folded into what is searched, not just the key"
