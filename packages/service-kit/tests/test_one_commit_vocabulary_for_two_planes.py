"""The five outcomes a failed Lance commit can have, and the ORDER they must be tested in.

Two planes classified the same condition and drifted: the catalog carried the full taxonomy while
`lancekit/writer.py` carried two markers and could not see the non-retryable case at all. They cannot
share a classifier — each raises its own plane's error type — so they share the VERDICT, and this pins
what the verdict means.

THE ORDERING IS THE LOAD-BEARING PART. Lance's retryable and incompatible messages both contain the
word `concurrent`, so a flat set of markers answers the dangerous one with the safe one's remedy.
"""

from __future__ import annotations

import pytest

from service_kit.lancekit.commit_verdict import CommitVerdict, classify_commit_failure


#: Real message shapes, not invented ones: the retryable text is what six concurrent `add_columns`
#: produced on this estate 2026-09-07, and the rest are the phrases each marker set was derived from.
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Retryable commit conflict for version 2: this Merge transaction was preempted by concurrent transaction", CommitVerdict.RETRYABLE_CONFLICT),
        ("Commit failed: incompatible transaction — a concurrent Overwrite replaced the table", CommitVerdict.INCOMPATIBLE),
        ("Append with different schema: `s` should have type string but type was int64", CommitVerdict.CLIENT_ERROR),
        ("Dataset must already exist unless mode is create", CommitVerdict.NO_BASE),
        ("connection reset by peer", CommitVerdict.STORE_UNAVAILABLE),
    ],
)
def test_each_outcome_is_named_from_the_message_lance_actually_sends(message: str, expected: CommitVerdict) -> None:
    assert classify_commit_failure(OSError(message)) is expected, f"{message!r} classified wrongly"


def test_INCOMPATIBLE_beats_the_bare_word_concurrent() -> None:
    """The defect this module was extracted to make unrepeatable.

    An incompatible transaction means the table was REPLACED underneath the writer, so the delta
    describes rows that no longer belong and re-sending it corrupts the table. Its message mentions
    concurrency, so a classifier testing the generic conflict markers first answers it "re-read and
    re-send" — which is exactly what `translate_commit_conflict` did until 2026-09-08.
    """
    both = OSError("commit conflict: incompatible transaction, preempted by concurrent transaction")
    assert classify_commit_failure(both) is CommitVerdict.INCOMPATIBLE, (
        "a message carrying BOTH vocabularies was read as retryable — the caller is being told to do the one thing that corrupts the table"
    )


def test_an_unknown_phrase_fails_toward_the_store_not_the_caller() -> None:
    """The safe direction for a new store's wording: a caller told "the store is unavailable" retries
    later and loses nothing; one told "your input is wrong" stops and loses the work."""
    assert classify_commit_failure(OSError("some wording no marker anticipated")) is CommitVerdict.STORE_UNAVAILABLE
