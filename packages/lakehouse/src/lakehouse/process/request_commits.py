"""Whether the running request's write has begun its commit, by which its request deadline answers (ADR 0203, amended
2026-11-28).

Two parts of a request meet here without knowing each other. The request deadline (`lakehouse.service_app.deadline`)
answers a caller whose door outlives it, and a write reaches its commit through code that does not take ``request`` (the
catalog's ``Database.writing()``). So the deadline gives each request a :class:`CommitMark` in its door's own context
(:func:`marking`), and a write reports its commit there just before it begins (:func:`commit_begins`). Past the deadline,
a request whose commit had begun is answered as one that may have applied (500, code 18), and one whose commit had not is
answered 503 and closed: a commit it would begin later is refused (:class:`LateCommitRefusedError`), so the write rolls
back and the 503 stays true for the catalog's database. The door is never cancelled; it meets the refusal as an exception,
and its own ``except`` blocks undo what it holds. A write that reports to no mark is not covered: a Lance commit, which
goes to the object store, and an OpenFGA write.

The mark lives in the door's context, so every task or thread the door starts copies it, and a task that outlived the
request would report its later commits to a request already answered and have them refused. A long-lived task is
therefore started by the process, never from a request, as the request's trace and its open transaction require too.
"""

import threading
from contextvars import ContextVar
from typing import Final


class LateCommitRefusedError(RuntimeError):
    """A write's commit refused because its request was already answered at its deadline, with no commit begun."""


class CommitMark:
    """One request's commit: whether a write of it has begun one, and whether its caller was answered with none begun."""

    def __init__(self) -> None:
        # A door may report from a worker thread it runs part of its work on, while the deadline settles on the event loop.
        self._lock = threading.Lock()
        self._begun = False
        self._closed = False

    @property
    def begun(self) -> bool:
        """Whether a write of this request has begun its commit."""
        with self._lock:
            return self._begun

    def begin(self) -> None:
        """Record that a write of this request begins its commit.

        Raises:
            LateCommitRefusedError: The request was answered at its deadline before this commit began.
        """
        with self._lock:
            if self._closed:
                raise LateCommitRefusedError(
                    "the request was answered at its deadline before this commit began, so the commit is refused and the write rolls back"
                )
            self._begun = True

    def settle(self) -> bool:
        """Whether a write of this request had begun its commit; if none had, close the request to every commit from now on.

        One step, so no commit can begin between the answer this settles and the closing.
        """
        with self._lock:
            if not self._begun:
                self._closed = True
            return self._begun


#: The running request's mark, set in its door's own context by the request deadline; none outside such a request.
_MARK: Final[ContextVar[CommitMark | None]] = ContextVar("request_commit_mark", default=None)


def marking(mark: CommitMark) -> None:
    """Give the running context ``mark``, to which every write in it reports its commit."""
    _MARK.set(mark)


def commit_begins() -> None:
    """Report that a write of the running request begins its commit; outside a request with a mark, nothing.

    The mark stays once set: a request that committed a write, or began to, is never answered as retryable.

    Raises:
        LateCommitRefusedError: The request was answered at its deadline before this commit began.
    """
    if (mark := _MARK.get()) is not None:
        mark.begin()


__all__ = ["CommitMark", "LateCommitRefusedError", "commit_begins", "marking"]
