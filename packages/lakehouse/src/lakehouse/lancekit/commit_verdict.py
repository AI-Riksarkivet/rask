"""ONE vocabulary for what a failed Lance commit MEANS, shared by every lakehouse plane that has to answer it.

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

THE TYPE FIRST, LANCE'S WORDING WHERE IT RAISES NO TYPE. pylance 13.0.0 raises `lance.commit.CommitConflictError`, whose
`retryable` says which conflict the commit lost, from `add_columns` and `LanceDataset.commit`; every other path raises a
plain `OSError` carrying Lance's wording: the retryable conflicts of `drop_columns` and a compaction, and the
incompatible ones of `delete`, `update`, `merge_insert`, `insert` and `write_dataset` (measured 2026-11-12). The same bad
predicate arrives as a `ValueError` from `update()` and an `OSError` from `delete()`. A phrase that is not listed falls
through to `STORE_UNAVAILABLE`, which is the safe direction for a new store's wording: a caller told
"the store is unavailable" retries later and loses nothing, where one told "your input is wrong" stops.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

from lance.commit import CommitConflictError


__all__ = ["CommitVerdict", "classify_commit_failure", "is_retryable_conflict"]


class CommitVerdict(Enum):
    """What a failed commit means, independent of which plane's error type will carry it.

    A plain ``Enum``: this is dispatched on, never serialised, so a ``str`` base would advertise an interchange format nothing
    reads. Not a ``Literal`` alias — the estate's idiom for a closed set of strings that CROSS a
    boundary (`ControlAction`, `RunStatus`, `Rung`) — because these values cross none.
    """

    #: An append whose read_version has no committed base — a declared-only table, or a version
    #: compacted away. A CLIENT error: otherwise a client appending to a freshly-declared table
    #: (read_version=0) gets a 503 and retries the same version forever.
    NO_BASE = "no_base"
    #: The caller's own data does not fit the table — wrong schema, unexpected fields, same version,
    #: V1 and V2 data files mixed.
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
_CLIENT_ERROR = (
    "different schema",
    "fields did not match",
    "same version",
    "invalid input",
    # pylance 12.0.0's file-version refusals at commit, pinned by `test_a_foreign_file_version_is_named.py`.
    # Both read "Invalid user input", which "invalid input" misses.
    "mixes v1 and v2 data files",
    "unknown lance storage version",
)
_INCOMPATIBLE = ("incompatible transaction",)
_CONFLICT = ("commit conflict", "concurrent")


#: How Lance words a retryable conflict it raises untyped, its own phrase and none of the broad markers: a column a
#: caller named ``concurrent`` is missing, not raced.
_LANCE_RETRYABLE_CONFLICT: Final = "retryable commit conflict"


def is_retryable_conflict(exc: BaseException) -> bool:
    """Whether Lance names ``exc`` a retryable commit conflict: by its type where it raises one, else in its own words."""
    if isinstance(exc, CommitConflictError):
        return exc.retryable
    return _LANCE_RETRYABLE_CONFLICT in str(exc).lower()


def classify_commit_failure(exc: BaseException) -> CommitVerdict:
    """What this commit failure means. Each plane maps the verdict to its own error type."""
    if isinstance(exc, CommitConflictError):
        return CommitVerdict.RETRYABLE_CONFLICT if exc.retryable else CommitVerdict.INCOMPATIBLE
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
