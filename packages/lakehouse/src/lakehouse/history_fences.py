"""Who may change a table's history right now: a reclaim that deletes its versions, or a writer of its tags and branches ([[LH-308]]).

LANCE READS A TABLE'S REFS ONCE PER RECLAIM. ``cleanup_old_versions`` lists the referenced branches and the tags when it
starts and deletes against that listing (``rust/lance/src/dataset/cleanup.rs:472-511`` at v13.0.0), and
``lance_docs/lance/guide/tags_and_branches.md:63-64`` and ``:131-133`` promise only that a tagged version is exempt and that cleanup keeps a
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

Every method is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from lakehouse import control_prefixes, records
from lakehouse.record_store import record_key
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

PREFIX: Final = control_prefixes.HISTORY_FENCES


def fence_key(table_uuid: str) -> str:
    """The key of the table ``table_uuid``'s fence record on the control root."""
    return record_key(PREFIX, "table", table_uuid)


#: How long a take, and each renewal of it, holds the fence: three renewal intervals, and short enough that a holder that
#: crashed does not keep a table's refs, or its reclaim, waiting long.
HOLD: Final = timedelta(minutes=15)


def _renewal_seconds() -> float:
    """How often a holder renews its hold while its work runs: every third of :data:`HOLD`."""
    return HOLD.total_seconds() / 3


#: How many read-and-write rounds a take or a release runs against holders of its own kind writing at once.
_ATTEMPTS: Final = 8


class HistoryWork(StrEnum):
    """What a holder does to the table's history."""

    #: Deletes versions: ``cleanup_old_versions`` in any of its callers.
    RECLAIM = "reclaim"
    #: Writes a tag or moves a branch's ref, each of which pins a version against a reclaim.
    REFS = "refs"
    #: Creates a branch: ref work no other branch create may share, since each judges the branch names the table holds.
    BRANCH = "branch"


def _excludes(held: HistoryWork, work: HistoryWork) -> bool:
    """Whether a holder doing ``held`` refuses a take for ``work``: work of the other kind, or a second branch create."""
    if held is work:
        return work is HistoryWork.BRANCH
    return HistoryWork.RECLAIM in (held, work)


class Holder(BaseModel):
    """One take of the fence."""

    token: str
    work: HistoryWork
    taken_at: datetime
    expires_at: datetime


class HistoryFence(BaseModel):
    """The fence of the table ``uuid``: its live holders, none of which excludes another."""

    uuid: str
    holders: list[Holder] = Field(default_factory=list)


class HistoryFencedError(Exception):
    """The table's history is held by work that excludes this take's, or by too many writers at once to join."""


def _live(record: dict[str, object] | None, now: datetime) -> list[Holder]:
    if record is None:
        return []
    return [holder for holder in HistoryFence.model_validate(record).holders if holder.expires_at > now]


class TableHistory(BaseModel):
    """Where one table's history fence is kept: the control root, the credential that writes there, and the table's uuid."""

    model_config = ConfigDict(frozen=True)

    control_root: str
    storage_options: StorageOptions
    table_uuid: str

    @property
    def key(self) -> str:
        """The fence record's key on the control root."""
        return fence_key(self.table_uuid)

    def take(self, work: HistoryWork) -> str:
        """Hold the table's history for ``work``; the token :meth:`release` ends the hold with.

        Raises:
            HistoryFencedError: Work that excludes ``work`` holds it, or the take lost every round to writers it shares with.
            botocore.exceptions.ClientError: The store refused a read or a write.
        """
        for _ in range(_ATTEMPTS):
            read = records.read_json_at(self.control_root, self.storage_options, self.key)
            now = read.at
            holders = _live(read.stored.record if read.stored else None, now)
            if other := next((holder for holder in holders if _excludes(holder.work, work)), None):
                raise HistoryFencedError(
                    f"a {other.work} holds this table's history until {other.expires_at.isoformat()}, so no {work} may start; retry once it ends"
                )
            mine = Holder(token=uuid.uuid4().hex, work=work, taken_at=now, expires_at=now + HOLD)
            fence = HistoryFence(uuid=self.table_uuid, holders=[*holders, mine]).model_dump(mode="json")
            try:
                if read.stored is None:
                    records.create_json(self.control_root, self.storage_options, self.key, fence)
                else:
                    records.replace_json(self.control_root, self.storage_options, self.key, fence, etag=read.stored.etag)
            except (records.RecordExistsError, records.RecordChangedError):
                continue
            return mine.token
        raise HistoryFencedError(f"{_ATTEMPTS} attempts to hold this table's history for a {work} each lost to another writer; retry")

    def release(self, token: str) -> None:
        """End the hold ``token`` names. Never raises: a hold this cannot end lapses after :data:`HOLD`."""
        try:
            for _ in range(_ATTEMPTS):
                read = records.read_json_at(self.control_root, self.storage_options, self.key)
                now = read.at
                if read.stored is None:
                    return
                fence = HistoryFence.model_validate(read.stored.record)
                mine = next((holder for holder in fence.holders if holder.token == token), None)
                if mine is None:
                    log.warning("history_fence_hold_gone", extra={"table_uuid": self.table_uuid})
                    return
                if mine.expires_at <= now:
                    log.error("history_fence_outlived", extra={"table_uuid": self.table_uuid, "work": mine.work, "held_since": mine.taken_at.isoformat()})
                kept = fence.model_copy(update={"holders": [holder for holder in fence.holders if holder.token != token and holder.expires_at > now]})
                try:
                    records.replace_json(self.control_root, self.storage_options, self.key, kept.model_dump(mode="json"), etag=read.stored.etag)
                except records.RecordChangedError:
                    continue
                return
            log.warning("history_fence_not_released", extra={"table_uuid": self.table_uuid, "reason": "every attempt lost to another writer"})
        except Exception as exc:  # noqa: BLE001 — a release runs in a `finally`, and must not replace the work's own outcome
            log.warning("history_fence_not_released", extra={"table_uuid": self.table_uuid, "reason": f"{type(exc).__name__}: {exc}"[:300]})

    def renew(self, token: str) -> bool:
        """Hold the table's history for :data:`HOLD` more from the store's clock now, under the take ``token`` names;
        ``False`` when that take holds it no longer.

        A hold already past its end is not renewed: another take may have been admitted since, judged against the holders
        that stood then, and only a fresh take may hold the table again.

        Raises:
            botocore.exceptions.ClientError: The store refused a read or a write.
            records.RecordChangedError: Every attempt lost to another writer of the fence.
        """
        for _ in range(_ATTEMPTS):
            read = records.read_json_at(self.control_root, self.storage_options, self.key)
            now = read.at
            if read.stored is None:
                return False
            fence = HistoryFence.model_validate(read.stored.record)
            live = [holder for holder in fence.holders if holder.expires_at > now]
            mine = next((holder for holder in live if holder.token == token), None)
            if mine is None:
                return False
            others = [holder for holder in live if holder.token != token]
            renewed = fence.model_copy(update={"holders": [*others, mine.model_copy(update={"expires_at": now + HOLD})]})
            try:
                records.replace_json(self.control_root, self.storage_options, self.key, renewed.model_dump(mode="json"), etag=read.stored.etag)
            except records.RecordChangedError:
                continue
            return True
        raise records.RecordChangedError(f"{_ATTEMPTS} attempts to renew a hold of this table's history each lost to another writer")

    def _keep(self, token: str, stopped: threading.Event) -> None:
        """Renew the take ``token`` names every third of :data:`HOLD` until ``stopped`` is set. Never raises: a renewal that
        fails is tried again at the next interval, still inside the hold, and one that finds the hold gone ends the
        renewals, logged."""
        while not stopped.wait(_renewal_seconds()):
            try:
                held = self.renew(token)
            except Exception as exc:  # noqa: BLE001 — retried at the next interval, which still falls inside the hold
                log.warning("history_fence_not_renewed", extra={"table_uuid": self.table_uuid, "reason": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            if held:
                continue
            if not stopped.is_set():
                log.error("history_fence_lost", extra={"table_uuid": self.table_uuid})
            return

    @contextmanager
    def held(self, work: HistoryWork) -> Generator[None]:
        """The table's history held for ``work`` while the block runs, the hold renewed for as long as it runs.

        The renewals stop before the release, waiting at most one renewal interval for one in flight: a renewal landing
        after the release reads the record the release wrote, under which its take holds nothing, and renews nothing.

        Raises:
            HistoryFencedError: Work that excludes ``work`` holds it; the block did not run.
            botocore.exceptions.ClientError: The store refused the take.
        """
        token = self.take(work)
        stopped = threading.Event()
        keeper = threading.Thread(target=self._keep, args=(token, stopped), name=f"history-fence-{self.table_uuid}", daemon=True)
        keeper.start()
        try:
            yield
        finally:
            stopped.set()
            keeper.join(timeout=_renewal_seconds())
            self.release(token)
