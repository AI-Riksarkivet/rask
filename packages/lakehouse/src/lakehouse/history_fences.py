"""Who may change a table's history right now: a reclaim that deletes its versions, or a writer of its tags and branches ([[LH-308]]).

LANCE READS A TABLE'S REFS ONCE PER RECLAIM. ``cleanup_old_versions`` lists the referenced branches and the tags when it
starts and deletes against that listing (``rust/lance/src/dataset/cleanup.rs:472-511`` at v13.0.0), and
``lance_docs/lance/guide/tags_and_branches.md:79-80`` and ``:131-133`` promise only that a tagged version is exempt and that cleanup keeps a
branch's files. Nothing in Lance orders a ref written meanwhile with the reclaim. Measured on pylance 13.0.0 with a tag
or branch created on version 2 a few milliseconds into ``cleanup_old_versions(older_than=0)`` on a local store: the tag
named a deleted version in 20 of 60 races, and the branch could not be read in 9 of 60.

SO THE TWO NEVER OVERLAP ON ONE TABLE. Each takes the table's history fence first (:class:`TableHistory`): one record
per table on the control root, keyed by the table's uuid (ADR 0173 §8), listing who holds it now. Holders of one kind
share it, since what the fence closes is a ref written while a reclaim runs, never two reclaims or two ref writes; a
holder of the other kind refuses the take (:class:`HistoryFencedError`), and the caller answers 409 or leaves its
reclaim for the next tick. The store picks the
winner: a take is a create-if-absent or a replace under the ETag it read (:mod:`lakehouse.records`), as a
location claim's is.

AN ERASURE'S RELEASE HOLDS IT ALONE (ADR 0233). The release deletes the refs standing on an erased subject's versions,
recreates the branches it deleted and reclaims what they kept, and a ref written anywhere in that span could pin a
version it is about to delete or keep a residual it has just let go: :attr:`HistoryWork.RELEASE` excludes every other
kind, and its own.

A BRANCH CREATE HOLDS IT ALONE ([[LH-413]]). Lance joins a branch name onto ``tree/`` verbatim and keeps each ref in its own
file (``lance_docs/lance/format/table/branch_tag.md`` § Branch Dataset Layout, § Branch Metadata Path), so ``a`` and
``a/b`` are two keys no conditional write on a ref can make collide, while ``a/b``'s files would lie inside ``a``'s. The
catalog refuses such a pair at create, judging the table's branch listing; :attr:`HistoryWork.BRANCH` is the one kind
that excludes its own, so two creates on one table take turns and each judges a listing no other create changes
before it commits.

A HOLD IS KEPT FOR AS LONG AS ITS WORK RUNS ([[LH-575]]). A reclaim can run longer than one :data:`HOLD` (maintenance
ends a pass's replica only at twice its 1,800 s bound), and a hold that ran out under it would admit a tag on a version
it is deleting, so :meth:`TableHistory.held` renews its hold every third of :data:`HOLD` while its block runs: two
renewals that fail in a row leave a third inside the hold. A holder that crashed renews nothing, so it fences the table for at most
:data:`HOLD` after its last renewal. A renewal that finds its hold gone, or a release that finds it ended, logs
``history_fence_lost`` or ``history_fence_outlived`` at ERROR. A renewal is written under the credential the take was,
so a holder whose credential expires (a maintenance vend lasts 900 s) can renew no further, and the deletes it signs
with that credential stop with it. Every hold is stamped and judged on the store's own clock
(:attr:`lakehouse.records.StoreRead.at`, the ``Date`` of the read before each write), never a pod's, so a holder's lapse
does not depend on two pods' clocks agreeing.

The holding, renewing and releasing are :class:`lakehouse.fences.Fence`'s; this module says which kinds exclude which.

Every method is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import ClassVar, Final

from lakehouse import control_prefixes
from lakehouse.fences import FenceRecord, Holder, RecordFence
from lakehouse.record_store import record_key


PREFIX: Final = control_prefixes.HISTORY_FENCES

__all__ = ["HOLD", "PREFIX", "HistoryFence", "HistoryFencedError", "HistoryWork", "Holder", "TableHistory", "fence_key"]


def fence_key(table_uuid: str) -> str:
    """The key of the table ``table_uuid``'s fence record on the control root."""
    return record_key(PREFIX, "table", table_uuid)


#: How long a take, and each renewal of it, holds the fence: three renewal intervals, and short enough that a holder that
#: crashed does not keep a table's refs, or its reclaim, waiting long.
HOLD: Final = timedelta(minutes=15)


class HistoryWork(StrEnum):
    """What a holder does to the table's history."""

    #: Deletes versions: ``cleanup_old_versions`` in any of its callers.
    RECLAIM = "reclaim"
    #: Writes a tag or moves a branch's ref, each of which pins a version against a reclaim.
    REFS = "refs"
    #: Creates a branch: ref work no other branch create may share, since each judges the branch names the table holds.
    BRANCH = "branch"
    #: An erasure's release: deletes refs, recreates branches and reclaims, so it shares the table with no other work.
    RELEASE = "release"


def _excludes(held: HistoryWork, work: HistoryWork) -> bool:
    """Whether a holder doing ``held`` refuses a take for ``work``: work of the other kind, a second branch create, or
    anything beside a release."""
    if HistoryWork.RELEASE in (held, work):
        return True
    if held is work:
        return work is HistoryWork.BRANCH
    return HistoryWork.RECLAIM in (held, work)


class HistoryFence(FenceRecord):
    """The fence's record, naming its table by uuid: the record every take, renewal and release reads and writes."""

    uuid: str


class HistoryFencedError(Exception):
    """The table's history is held by work that excludes this take's, or by too many writers at once to join."""


class TableHistory(RecordFence):
    """Where one table's history fence is kept: the control root, the credential that writes there, and the table's uuid.

    One record per table, since a maintain-tier vend may write that key of the control root and no other
    (``catalog.core.vending.TableRecords``): ref writes on one table contend for it, and a take that loses every round to
    them is refused. It ends with the table's other records (:mod:`lakehouse.object_records`).
    """

    log_name: ClassVar[str] = "history_fence"
    stored_field: ClassVar[str] = "uuid"
    record_model: ClassVar[type[FenceRecord]] = HistoryFence

    table_uuid: str

    @property
    def key(self) -> str:
        return fence_key(self.table_uuid)

    @property
    def subject(self) -> str:
        return self.table_uuid

    @property
    def what(self) -> str:
        return "this table's history"

    def hold(self) -> timedelta:
        return HOLD

    def excludes(self, held: str, work: str) -> bool:
        return _excludes(HistoryWork(held), HistoryWork(work))

    def refused(self, message: str) -> Exception:
        return HistoryFencedError(message)
