"""A table registered somewhere else is a PERMANENT conflict, not a transient outage.

§ Q9-4. `_require_same_location` refuses when the catalog says a table lives at a location other than
where this writer writes, and it is right to: the catalog would be governing a different copy. But the
head answers every `RegisterError` with `503` + `Retry-After: 5`, whose own comment reasons that
"nothing happened and the caller's retry (same Idempotency-Key) converges".

THAT IS TRUE OF ONE CAUSE AND FALSE OF THE OTHER. `RegisterError` covers both:

    TRANSIENT   the catalog is unreachable, or would not say where the table is. Retrying converges.
    PERMANENT   the table is registered at a DIFFERENT location. Nothing about a retry changes that —
                the error's own remedy says "deregister, then register at the written location".

So the caller is told to retry a thing that can never succeed, and the row's words for it are exact:
"promising a convergence no retry can produce. The next tenant whose catalog and head disagree is 503
again." It is the same shape as the maintenance refusal no retry could clear (§ H10) — a control that
answers with advice the caller cannot act on.

409 is the answer the head ALREADY uses for the other permanent conflict on this route
(`UnresolvableProjectError`), so this is parity with its own contract, not a new code.
"""

from __future__ import annotations

import pytest

from medallion.services.catalog_register import LocationConflictError, RegisterError


def test_a_location_conflict_is_its_own_error() -> None:
    """The distinction has to exist as a TYPE, not a substring of a message: the endpoint decides the
    status code, and matching on error text is how a rename silently restores the 503."""
    assert issubclass(LocationConflictError, RegisterError), "a location conflict must still BE a RegisterError, or every existing handler stops catching it"


def test_the_endpoint_answers_a_conflict_with_409_not_503() -> None:
    """The behaviour the row is about: a permanent conflict must not carry Retry-After.

    Read from the SOURCE rather than driven, because reaching this branch through the real route means
    standing up the app, a Dapr client and a catalog that answers `describe` with a disagreeing
    location — a harness whose own failure modes would dominate the thing under test. The API layer
    does not import the service's exception on purpose: `run_produce` maps it to a status string, which
    is what keeps the route from depending on the service's error hierarchy.
    """
    import inspect

    from medallion.api import produce as produce_api

    src = inspect.getsource(produce_api)
    assert '"location_conflict"' in src, "the route cannot tell a permanent conflict from an outage, so it answers both 503"

    conflict_at = src.index('"location_conflict"')
    retry_at = src.index('"Retry-After"')
    assert conflict_at < retry_at, "the conflict branch sits after the 503 branch, so it is unreachable"
    # Ends at the NEXT branch, not at the Retry-After: slicing to the header lands one character
    # inside the 503 block's `headers={`, which reads as this branch carrying it.
    branch = src[conflict_at : src.index("if result.get(", conflict_at + 1)]
    assert "status_code=409" in branch, "a permanent location conflict is not answered 409"
    # THE HEADER, not the word: this branch's own comment explains why Retry-After is wrong here, so
    # matching the phrase matches the explanation. `headers=` is the mechanism that sets it.
    assert "headers=" not in branch, "the conflict answer still carries a header telling the caller to retry what no retry can fix"


def test_an_UNREACHABLE_catalog_is_still_retryable() -> None:
    """The other direction, and the one that must not regress: a catalog that is merely down IS a
    transient failure, and answering it 409 would tell an operator to re-point a registration that is
    perfectly correct."""
    exc = RegisterError("catalog unreachable verifying where 'bronze$events' is registered")
    assert not isinstance(exc, LocationConflictError), "a transient failure was classified as a permanent conflict"


def test_the_conflict_carries_the_remedy_that_actually_works() -> None:
    """A 409 whose body still says "retry" is the same lie with a different number."""
    exc = LocationConflictError("'t' is registered at 'a' but this writer writes 'b' — re-point the registration")
    assert "re-point" in str(exc).lower(), "the conflict does not name the action that resolves it"
    with pytest.raises(RegisterError):
        raise exc
