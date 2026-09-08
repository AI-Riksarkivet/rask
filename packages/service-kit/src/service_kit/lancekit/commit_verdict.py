"""ONE vocabulary for what a failed Lance commit MEANS, shared by every plane that has to answer it.

Two planes classify the same condition and they disagreed. The catalog carries the full taxonomy
(`catalog/services/dataplane.py`, audit 2026-07-14) and maps it to `lance_namespace` errors;
`lancekit/writer.py` carried two markers and mapped them to `DomainError`s. The vocabularies could not
simply be merged, because each plane must raise ITS OWN error type — so what is shared is the VERDICT,
and each plane maps it.

WHY THE SPLIT MATTERED RATHER THAN BEING UNTIDY. Lance's messages for the two conflict outcomes both
mention concurrency, and only one is safe to retry:

    RETRYABLE       "Retryable commit conflict for version 2: this Merge transaction was preempted
                     by concurrent transaction ..."      -> re-read, rebuild, re-send
    INCOMPATIBLE    "... incompatible transaction ..."     -> the table changed underneath the writer
                                                             (a concurrent Overwrite REPLACED its
                                                             contents); the delta describes rows that
                                                             no longer belong

`writer.py` matched the bare word `concurrent`, which appears in both, so an INCOMPATIBLE transaction
was answered `409 re-read and re-send` — the one thing the caller must not do, and what the catalog's
own classifier calls "how you corrupt a table". Ordering the markers is the whole fix, and it is why
this is a shared ordered function rather than a shared tuple of strings.

TEXT, NOT TYPES, and that is forced: pylance 9.0.0 exposes no typed error for a lost commit race
(probed — neither `lance.error` nor the native module carries one), and the same bad predicate arrives
as a `ValueError` from `update()` and an `OSError` from `delete()`. A phrase that is not listed falls
through to `STORE_UNAVAILABLE`, which is the safe direction for a new store's wording: a caller told
"the store is unavailable" retries later and loses nothing, where one told "your input is wrong" stops.
"""

from __future__ import annotations

from enum import Enum


__all__ = ["CommitVerdict", "classify_commit_failure"]


class CommitVerdict(Enum):
    """What a failed commit means, independent of which plane's error type will carry it.

    A plain ``Enum``, matching the estate's one other verdict type (`viewer`'s ``RangeVerdict``): this
    is dispatched on, never serialised, so a ``str`` base would advertise an interchange format nothing
    reads. Not a ``Literal`` alias — the estate's idiom for a closed set of strings that CROSS a
    boundary (`ControlAction`, `RunStatus`, `Rung`) — because these values cross none.
    """

    #: An append whose read_version has no committed base — a declared-only table, or a version
    #: compacted away. A CLIENT error: otherwise a client appending to a freshly-declared table
    #: (read_version=0) gets a 503 and retries the same version forever.
    NO_BASE = "no_base"
    #: The caller's own data does not fit the table — wrong schema, unexpected fields, same version.
    CLIENT_ERROR = "client_error"
    #: NON-RETRYABLE per the format spec's conflict taxonomy (transaction.md: an *Incompatible*
    #: conflict "fails with a non-retryable error"). Re-committing is what corrupts the table.
    INCOMPATIBLE = "incompatible"
    #: RETRYABLE contention — the losing side of a genuine race re-reads and re-commits safely.
    RETRYABLE_CONFLICT = "retryable_conflict"
    #: Anything else. The store, not the request.
    STORE_UNAVAILABLE = "store_unavailable"


#: Ordered most-specific first. `_CONFLICT` carries the bare word `concurrent`, which appears in the
#: incompatible message too, so `_INCOMPATIBLE` MUST be tested before it — that ordering is the defect
#: this module exists to make unrepeatable.
_NO_BASE = ("must already exist unless", "manifest was not found", "no such file")
_CLIENT_ERROR = ("different schema", "fields did not match", "same version", "invalid input")
_INCOMPATIBLE = ("incompatible transaction",)
_CONFLICT = ("commit conflict", "concurrent")


def classify_commit_failure(exc: BaseException) -> CommitVerdict:
    """What this commit failure means. Each plane maps the verdict to its own error type."""
    message = str(exc).lower()
    for markers, verdict in (
        (_NO_BASE, CommitVerdict.NO_BASE),
        (_CLIENT_ERROR, CommitVerdict.CLIENT_ERROR),
        (_INCOMPATIBLE, CommitVerdict.INCOMPATIBLE),
        (_CONFLICT, CommitVerdict.RETRYABLE_CONFLICT),
    ):
        if any(m in message for m in markers):
            return verdict
    return CommitVerdict.STORE_UNAVAILABLE
