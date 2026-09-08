"""A LOST COMMIT RACE on the direct write path must be a 409, not a 500 (#50).

The annotations version check and the write are two separate steps: the endpoint reads
`reader.table_version()`, builds a delta from it, then commits. A writer that lands in between
passes the check and loses the commit. That is the ordinary optimistic-concurrency outcome and the
caller's remedy is the same as for a stale `base_version` — re-read, rebuild, re-send.

Untranslated it escaped as a raw `OSError`, which no handler maps (`install_exception_handlers`
registers only `DomainError` and `RequestValidationError`), so the browser got a 500. A 500 reads as
"the server is broken" and is not retried, and the annotator's client only branches on 409
(`annotations-client.ts`) — so the one moment the guarantee actually fired was reported as an
outage, and the annotator's work was lost rather than merged.

pylance 9.0.0 exposes no typed error for this (probed: neither `lance.error` nor the native module
carries one), so the message is the only signal available. These pin that the matching stays honest
in both directions — a real conflict is translated, and an unrelated OSError is NOT swallowed.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest

from service_kit.exceptions import ConflictError, DomainError
from service_kit.lancekit.writer import translate_commit_conflict


def _fail_with(exc: OSError | None) -> None:
    """Raise through a CALL that CAN return, rather than a bare `raise` in the test body.

    `ty` runs with `error-on-warning = true` and does not model `pytest.raises` as catching, so a
    literal `raise` inside the block marks every following line "always unreachable" — which turns
    the assertion ON the caught error into a type-check failure. A helper alone is not enough
    either: a body that ALWAYS raises is inferred `NoReturn`, so calling it terminates the flow just
    the same. The `None` branch is what keeps the function ordinarily-returning and the assertions
    below reachable. Nothing about what is asserted is weakened — the tests still pass a real
    `OSError` every time.
    """
    if exc is not None:
        raise exc


@pytest.mark.parametrize(
    "message",
    [
        "Commit conflict for version 8",
        "commit conflict: retry the transaction",
        "Concurrent write detected on the dataset",
    ],
)
def test_a_lost_race_becomes_a_conflict(message: str) -> None:
    """The markers are the catalog classifier's own (`_COMMIT_CONFLICT_MARKERS` in
    `catalog/services/dataplane.py`), kept identical so one vocabulary covers one condition on
    whichever plane hits it. Matching is case-insensitive because Lance capitalises inconsistently."""
    with pytest.raises(ConflictError), translate_commit_conflict():
        _fail_with(OSError(message))


def test_the_conflict_names_the_REMEDY_not_just_the_fact() -> None:
    """ "Conflict" alone leaves a client unable to act. The annotator must re-read and re-send, and
    saying so is the difference between a recoverable refusal and an error someone reports as a bug."""
    with pytest.raises(ConflictError) as exc, translate_commit_conflict():
        _fail_with(OSError("Commit conflict for version 8"))

    assert "re-read" in str(exc.value).lower()


def test_an_UNRELATED_OSError_is_not_swallowed() -> None:
    """The dangerous direction. A disk failure, a permissions error or an object-store outage is NOT
    a lost race: calling it 409 would tell the annotator to retry a write that cannot succeed, and
    would hide a real outage behind a routine-looking refusal. Anything unmatched propagates."""
    with pytest.raises(OSError, match="No space left on device") as exc, translate_commit_conflict():
        _fail_with(OSError("No space left on device"))

    assert not isinstance(exc.value, ConflictError)


def test_a_clean_write_passes_through_untouched() -> None:
    """The guard must be invisible when nothing goes wrong — it wraps every direct write."""
    seen = []
    with translate_commit_conflict():
        seen.append("committed")

    assert seen == ["committed"]


def test_a_NON_RETRYABLE_conflict_is_never_advised_to_re_send() -> None:
    """The dangerous case, and the one this module could not see.

    Lance's conflict taxonomy has two outcomes that both mention concurrency, and only one is safe to
    retry. A RETRYABLE conflict is the ordinary OCC loss: re-read, rebuild, re-send. An INCOMPATIBLE
    transaction means the table changed underneath the writer — a concurrent Overwrite REPLACED its
    contents — and the catalog's own classifier says what advising a re-send there costs: "this is NOT
    retryable... do not re-commit", because the delta describes rows that no longer belong.

    `translate_commit_conflict` matched on the bare word `concurrent`, which appears in both messages,
    so an incompatible transaction was answered with a 409 whose remedy is the one thing the caller
    must not do. The catalog has ordered its markers for this since the 2026-07-14 audit; this plane
    never learned them.
    """
    incompatible = OSError("Commit failed: incompatible transaction — a concurrent Overwrite replaced the table")
    with pytest.raises(DomainError) as caught, translate_commit_conflict():
        raise incompatible
    assert not isinstance(caught.value, ConflictError), (
        "an INCOMPATIBLE transaction was answered 409 're-read and re-send' — the caller's delta describes rows the table no longer has"
    )
    assert caught.value.status_code == HTTPStatus.BAD_REQUEST, f"a non-retryable conflict answered {caught.value.status_code}, not 400"
    assert "not retryable" in str(caught.value.detail).lower(), "the answer does not tell the caller the one thing that matters: do not re-commit"


def test_a_RETRYABLE_conflict_still_says_re_send() -> None:
    """The other direction of the same ordering: the ordinary OCC loss must keep its 409, or a client
    that correctly branches on 409 stops retrying a save it should retry."""
    retryable = OSError("Retryable commit conflict for version 2: this Merge transaction was preempted by concurrent transaction")
    with pytest.raises(ConflictError), translate_commit_conflict():
        raise retryable
