"""Who may change a table's history right now: a reclaim that deletes its versions, or a writer of its tags and branches ([[LH-308]]).

LANCE READS A TABLE'S REFS ONCE PER RECLAIM. ``cleanup_old_versions`` lists the referenced branches and the tags when it
starts and deletes against that listing (``rust/lance/src/dataset/cleanup.rs:472-511`` at v13.0.0), and
``lance_docs/lance/guide/tags_and_branches.md:79-80`` and ``:131-133`` promise only that a tagged version is exempt and that cleanup keeps a
branch's files. Nothing in Lance orders a ref written meanwhile with the reclaim. Measured on pylance 13.0.0 with a tag
or branch created on version 2 a few milliseconds into ``cleanup_old_versions(older_than=0)`` on a local store: the tag
named a deleted version in 20 of 60 races, and the branch could not be read in 9 of 60.

SO THE TWO NEVER OVERLAP ON ONE TABLE. Each takes the table's history fence first (:class:`TableHistory`), kept as one
object per holder beneath the table's directory on the control root, keyed by the table's uuid (ADR 0173 §8,
:func:`fence_directory`). Holders of one kind share it, since what the fence closes is a ref written while a reclaim
runs, never two reclaims or two ref writes; a holder of the other kind refuses the take (:class:`HistoryFencedError`),
and the caller answers 409 or leaves its reclaim for the next tick. The store picks the winner
(:class:`lakehouse.fences.HolderFence`, ``specs/catalog/Fence.tla``).

REF WRITES NEVER REFUSE REF WRITES. Every tag and branch write takes the fence, so ref writes share it whatever their
number, each writing an object of its own. Measured on RustFS 1.0.3 with one record per table that every take replaced
under its ETag: 7 to 9 of 16, and 36 to 39 of 64, concurrent tag writes on one table answered 409 with nothing excluding
them (ADR 0233 §9).

AN ERASURE'S RELEASE HOLDS IT ALONE (ADR 0233). The release deletes the refs standing on an erased subject's versions,
recreates the branches it deleted and reclaims what they kept, and a ref written anywhere in that span could pin a
version it is about to delete or keep a residual it has just let go: :attr:`HistoryWork.RELEASE` excludes every other
kind, and its own.

AND ITS SCHEDULING EXCLUDES EVERY REF WRITE (ADR 0233 §9). Whoever puts a ref the release will take, once the release is
recorded, is told at that ref's write (``catalog.services.erasure_release``), which reads the release's mark in the
fence's directory off its own take's listing. The verification writes that mark and then the release's record while it
holds the fence as :attr:`HistoryWork.SCHEDULE`, which no ref write shares: a ref write either finished before the
record stood, or its take came after the mark and saw it.

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

from lakehouse import control_prefixes, stored_objects
from lakehouse.fences import Holder, HolderFence, HolderRecord
from lakehouse.record_store import record_name
from lakehouse.storage_options import StorageOptions


PREFIX: Final = control_prefixes.HISTORY_FENCES

__all__ = [
    "HOLD",
    "PREFIX",
    "RELEASE_MARK",
    "VICTIMS_MARK",
    "HistoryFencedError",
    "HistoryHolder",
    "HistoryWork",
    "Holder",
    "TableHistory",
    "end",
    "fence_directory",
]


def fence_directory(table_uuid: str) -> str:
    """The key of the directory of the table ``table_uuid``'s history fence on the control root: one object per holder,
    and the marks of the work coming to the table's history, beneath it."""
    return f"{PREFIX}/{record_name('table', table_uuid).removesuffix('.json')}"


def end(control_root: str, storage_options: StorageOptions, table_uuid: str) -> bool:
    """Delete every object of the table ``table_uuid``'s history fence, holders and marks, as the table ends; whether any
    stood. Writes no directory marker (:func:`stored_objects.delete_objects`).

    Raises:
        OSError: The store refused the listing or a delete.
    """
    listed = stored_objects.list_under(f"{control_root.rstrip('/')}/{fence_directory(table_uuid)}", storage_options)
    stored_objects.delete_objects(control_root, storage_options, [stored.path for stored in listed])
    return bool(listed)


#: How long a take, and each renewal of it, holds the fence: three renewal intervals, and short enough that a holder that
#: crashed does not keep a table's refs, or its reclaim, waiting long.
HOLD: Final = timedelta(minutes=15)


class HistoryWork(StrEnum):
    """What a holder does to the table's history."""

    #: Deletes versions: ``cleanup_old_versions`` in any of its callers.
    RECLAIM = "reclaim"
    #: Writes a tag or moves a branch's ref, each of which pins a version against a reclaim; a restore, which brings an old
    #: version's files back, and a table's policy set or delete, which a reclaim reads under its hold, hold it too (ADR 0243).
    REFS = "refs"
    #: Creates a branch: ref work no other branch create may share, since each judges the branch names the table holds.
    BRANCH = "branch"
    #: An erasure's release: deletes refs, recreates branches and reclaims, so it shares the table with no other work.
    RELEASE = "release"
    #: Writes an erasure release's mark and then its record: no ref write may land between the two unseen.
    SCHEDULE = "schedule"


#: The kind of an erasure release's mark in a table's fence directory (``catalog.services.erasure_release.mark_key``): the
#: one mark a take hands its holder, and one only the catalog's own credential can write.
RELEASE_MARK: Final = "erasure-release"

#: The kind of a cleanup's mark in a table's fence directory (:mod:`lakehouse.reclaim_marks`): the versions one cleanup will
#: remove, written under its RECLAIM hold before its first delete and removed once it returned with no failed delete. A
#: maintain-tier vend may put and delete one (``catalog.core.vending.TableRecords``); a ref door refuses a version one names.
VICTIMS_MARK: Final = "reclaim-victims"

#: The kinds that keep refs off the table's history while they hold it: they delete versions, or write what a ref write
#: must see before it lands. They share it with their own kind and with each other.
_REF_FREE: Final = frozenset({HistoryWork.RECLAIM, HistoryWork.SCHEDULE})
_REF_WRITES: Final = frozenset({HistoryWork.REFS, HistoryWork.BRANCH})


def _excludes(held: HistoryWork, work: HistoryWork) -> bool:
    """Whether a holder doing ``held`` refuses a take for ``work``: a ref write beside a reclaim or a scheduling, a second
    branch create, or anything beside a release."""
    if HistoryWork.RELEASE in (held, work):
        return True
    if held is work:
        return work is HistoryWork.BRANCH
    return {held, work} & _REF_FREE != set() and {held, work} & _REF_WRITES != set()


class HistoryHolder(HolderRecord):
    """One holder's object, naming the table it fences by uuid."""

    uuid: str


class HistoryFencedError(Exception):
    """The table's history is held by work that excludes this take's."""


class TableHistory(HolderFence):
    """Where one table's history fence is kept: the control root, the credential that writes there, and the table's uuid.

    A maintain-tier vend reaches this directory and no other of the control root: it lists it, reads every object in it,
    and puts and deletes every key below ``reclaim-`` (``catalog.core.vending.TableRecords``), which is a reclaim's holder
    objects and, since an IAM ``*`` matches ``/``, any name in a subdirectory below one. A take judges only the names this
    fence defines directly in the directory, a holder's kind by its key (:class:`lakehouse.fences.HolderFence`), so what
    the vend can plant is at most a reclaim's hold, for one hold; a lapsed holder of another kind, and whatever was planted,
    is swept by the catalog's next take. It ends with the table (:func:`end`).
    """

    log_name: ClassVar[str] = "history_fence"
    stored_field: ClassVar[str] = "uuid"
    holder_model: ClassVar[type[HolderRecord]] = HistoryHolder
    kinds: ClassVar[frozenset[str]] = frozenset(HistoryWork)
    mark_kinds: ClassVar[frozenset[str]] = frozenset({RELEASE_MARK, VICTIMS_MARK})

    table_uuid: str

    @property
    def directory(self) -> str:
        return fence_directory(self.table_uuid)

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
