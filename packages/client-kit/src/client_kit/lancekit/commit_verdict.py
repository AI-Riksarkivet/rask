"""What a failed Lance commit on a client's direct write path MEANS, so the caller is told the one right thing to do.

Lance's messages for the two conflict outcomes both mention concurrency, and only one is safe to retry:

    RETRYABLE       "Retryable commit conflict for version 2: this Merge transaction was preempted
                     by concurrent transaction ..."      -> re-read, rebuild, re-send
    INCOMPATIBLE    "... incompatible transaction ..."     -> the table changed underneath the writer
                                                             (a concurrent Overwrite REPLACED its
                                                             contents); the delta describes rows that
                                                             no longer belong

Matching the bare word ``concurrent`` answers an INCOMPATIBLE transaction "re-read and re-send", the one thing its
caller must not do, so the markers are tested most specific first. TEXT, NOT TYPES, and that is forced: pylance exposes
no typed error for a lost commit race (probed on 9.0.0), and the same bad predicate arrives as a ``ValueError`` from
``update()`` and an ``OSError`` from ``delete()``. A phrase that is not listed falls through to ``STORE_UNAVAILABLE``, the
safe direction: a caller told the store is unavailable retries later and loses nothing, where one told its input is
wrong stops.
"""

from __future__ import annotations

from enum import Enum


__all__ = ["CommitVerdict", "classify_commit_failure"]


class CommitVerdict(Enum):
    """What a failed commit means; the caller maps the verdict to its own error type."""

    #: An append whose read_version has no committed base: a declared-only table, or a version compacted away.
    NO_BASE = "no_base"
    #: The caller's own data does not fit the table: wrong schema, unexpected fields, same version, V1 and V2 data files mixed.
    CLIENT_ERROR = "client_error"
    #: NON-RETRYABLE per the format spec's conflict taxonomy (`lance_docs/lance/format/table/transaction.md`: an
    #: *Incompatible* conflict "fails with a non-retryable error"). Re-committing is what corrupts the table.
    INCOMPATIBLE = "incompatible"
    #: RETRYABLE contention: the losing side of a genuine race re-reads and re-commits safely.
    RETRYABLE_CONFLICT = "retryable_conflict"
    #: Anything else. The store, not the request.
    STORE_UNAVAILABLE = "store_unavailable"


#: Ordered most specific first: `_CONFLICT` carries the bare word `concurrent`, which the incompatible message carries
#: too, so `_INCOMPATIBLE` is tested before it.
_NO_BASE = ("must already exist unless", "manifest was not found", "no such file")
_CLIENT_ERROR = (
    "different schema",
    "fields did not match",
    "same version",
    "invalid input",
    # pylance 12.0.0's file-version refusals at commit, both reading "Invalid user input", which "invalid input" misses.
    "mixes v1 and v2 data files",
    "unknown lance storage version",
)
_INCOMPATIBLE = ("incompatible transaction",)
_CONFLICT = ("commit conflict", "concurrent")


def classify_commit_failure(exc: BaseException) -> CommitVerdict:
    """What this commit failure means."""
    message = str(exc).lower()
    for markers, verdict in (
        (_NO_BASE, CommitVerdict.NO_BASE),
        (_CLIENT_ERROR, CommitVerdict.CLIENT_ERROR),
        (_INCOMPATIBLE, CommitVerdict.INCOMPATIBLE),
        (_CONFLICT, CommitVerdict.RETRYABLE_CONFLICT),
    ):
        if any(marker in message for marker in markers):
            return verdict
    return CommitVerdict.STORE_UNAVAILABLE
