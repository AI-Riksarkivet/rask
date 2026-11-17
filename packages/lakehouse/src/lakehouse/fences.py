"""A fence on the control root: one record per object listing who holds it now, each holder's kind of work admitted only
beside work it shares with.

Two fences are built on it: a table's history (:mod:`lakehouse.history_fences`, a reclaim against a write of its refs)
and a namespace's rows (:mod:`lakehouse.namespace_fences`, a drop against an add beneath it). Each says which kinds of
work exclude which (:meth:`Fence.excludes`), how long a hold lasts, and how a refusal reads.

THE STORE PICKS THE WINNER. A take is a create-if-absent or a replace under the ETag it read (:mod:`lakehouse.records`),
so two takes that exclude each other cannot both land: the one that writes second reads the first, and refuses itself.

A HOLD IS KEPT FOR AS LONG AS ITS WORK RUNS ([[LH-575]]). :meth:`Fence.held` renews its hold every third of the hold
while its block runs: two renewals that fail in a row leave a third inside the hold. A holder that crashed renews nothing,
so it fences the object for at most one hold after its last renewal. A renewal that finds its hold gone, or a release
that finds it ended, logs ``<name>_lost`` or ``<name>_outlived`` at ERROR. A renewal is written under the credential the
take was, so a holder whose credential expires can renew no further. Every hold is stamped and judged on the store's own
clock (:attr:`lakehouse.records.StoreRead.at`, the ``Date`` of the read before each write), never a pod's, so a holder's
lapse does not depend on two pods' clocks agreeing.

Every method is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

from lakehouse import records
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: How many read-and-write rounds a take or a release runs against holders of its own kind writing at once.
_ATTEMPTS: Final = 8


class Holder(BaseModel):
    """One take of a fence."""

    token: str
    work: str
    taken_at: datetime
    expires_at: datetime


class FenceRecord(BaseModel):
    """A fence's record: what it fences, and its live holders, none of which excludes another."""

    subject: str
    holders: list[Holder] = Field(default_factory=list)


class Fence(BaseModel):
    """Where one object's fence is kept: the control root and the credential that writes there. A subclass names the
    object (:attr:`key`, :attr:`subject`), the kinds of work and which exclude which, how long a hold lasts, and the
    refusal it raises."""

    model_config = ConfigDict(frozen=True)

    control_root: str
    storage_options: StorageOptions

    #: The prefix of the events this fence logs: ``<name>_lost``, ``<name>_outlived`` and the rest.
    log_name: ClassVar[str] = "fence"

    @property
    def key(self) -> str:
        """The fence record's key on the control root."""
        raise NotImplementedError

    @property
    def subject(self) -> str:
        """What the record says it fences."""
        raise NotImplementedError

    @property
    def what(self) -> str:
        """What a refusal says is held."""
        raise NotImplementedError

    def hold(self) -> timedelta:
        """How long a take, and each renewal of it, holds the fence."""
        raise NotImplementedError

    def excludes(self, held: str, work: str) -> bool:
        """Whether a holder doing ``held`` refuses a take for ``work``."""
        raise NotImplementedError

    def refused(self, message: str) -> Exception:
        """The error a take refused for ``message`` raises."""
        raise NotImplementedError

    def _live(self, record: dict[str, object] | None, now: datetime) -> list[Holder]:
        if record is None:
            return []
        return [holder for holder in FenceRecord.model_validate(record).holders if holder.expires_at > now]

    def take(self, work: str) -> str:
        """Hold the fence for ``work``; the token :meth:`release` ends the hold with.

        Raises:
            Exception: :meth:`refused`'s error: work that excludes ``work`` holds it, or the take lost every round to writers
                it shares with.
            botocore.exceptions.ClientError: The store refused a read or a write.
        """
        for _ in range(_ATTEMPTS):
            read = records.read_json_at(self.control_root, self.storage_options, self.key)
            now = read.at
            holders = self._live(read.stored.record if read.stored else None, now)
            if other := next((holder for holder in holders if self.excludes(holder.work, work)), None):
                raise self.refused(f"a {other.work} holds {self.what} until {other.expires_at.isoformat()}, so no {work} may start; retry once it ends")
            mine = Holder(token=uuid.uuid4().hex, work=str(work), taken_at=now, expires_at=now + self.hold())
            fence = FenceRecord(subject=self.subject, holders=[*holders, mine]).model_dump(mode="json")
            try:
                if read.stored is None:
                    records.create_json(self.control_root, self.storage_options, self.key, fence)
                else:
                    records.replace_json(self.control_root, self.storage_options, self.key, fence, etag=read.stored.etag)
            except (records.RecordExistsError, records.RecordChangedError):
                continue
            return mine.token
        raise self.refused(f"{_ATTEMPTS} attempts to hold {self.what} for a {work} each lost to another writer; retry")

    def release(self, token: str) -> None:
        """End the hold ``token`` names. Never raises: a hold this cannot end lapses after :meth:`hold`."""
        try:
            for _ in range(_ATTEMPTS):
                read = records.read_json_at(self.control_root, self.storage_options, self.key)
                now = read.at
                if read.stored is None:
                    return
                fence = FenceRecord.model_validate(read.stored.record)
                mine = next((holder for holder in fence.holders if holder.token == token), None)
                if mine is None:
                    log.warning(f"{self.log_name}_hold_gone", extra={"subject": self.subject})
                    return
                if mine.expires_at <= now:
                    log.error(f"{self.log_name}_outlived", extra={"subject": self.subject, "work": mine.work, "held_since": mine.taken_at.isoformat()})
                kept = fence.model_copy(update={"holders": [holder for holder in fence.holders if holder.token != token and holder.expires_at > now]})
                try:
                    records.replace_json(self.control_root, self.storage_options, self.key, kept.model_dump(mode="json"), etag=read.stored.etag)
                except records.RecordChangedError:
                    continue
                return
            log.warning(f"{self.log_name}_not_released", extra={"subject": self.subject, "reason": "every attempt lost to another writer"})
        except Exception as exc:
            log.warning(f"{self.log_name}_not_released", extra={"subject": self.subject, "reason": f"{type(exc).__name__}: {exc}"[:300]})

    def renew(self, token: str) -> bool:
        """Hold the fence for one more hold from the store's clock now, under the take ``token`` names; ``False`` when that
        take holds it no longer.

        A hold already past its end is not renewed: another take may have been admitted since, judged against the holders
        that stood then, and only a fresh take may hold the fence again.

        Raises:
            botocore.exceptions.ClientError: The store refused a read or a write.
            records.RecordChangedError: Every attempt lost to another writer of the fence.
        """
        for _ in range(_ATTEMPTS):
            read = records.read_json_at(self.control_root, self.storage_options, self.key)
            now = read.at
            if read.stored is None:
                return False
            fence = FenceRecord.model_validate(read.stored.record)
            live = [holder for holder in fence.holders if holder.expires_at > now]
            mine = next((holder for holder in live if holder.token == token), None)
            if mine is None:
                return False
            others = [holder for holder in live if holder.token != token]
            renewed = fence.model_copy(update={"holders": [*others, mine.model_copy(update={"expires_at": now + self.hold()})]})
            try:
                records.replace_json(self.control_root, self.storage_options, self.key, renewed.model_dump(mode="json"), etag=read.stored.etag)
            except records.RecordChangedError:
                continue
            return True
        raise records.RecordChangedError(f"{_ATTEMPTS} attempts to renew a hold of {self.what} each lost to another writer")

    def _renewal_seconds(self) -> float:
        return self.hold().total_seconds() / 3

    def _keep(self, token: str, stopped: threading.Event) -> None:
        """Renew the take ``token`` names every third of a hold until ``stopped`` is set. Never raises: a renewal that fails
        is tried again at the next interval, still inside the hold, and one that finds the hold gone ends the renewals,
        logged."""
        while not stopped.wait(self._renewal_seconds()):
            try:
                held = self.renew(token)
            except Exception as exc:
                log.warning(f"{self.log_name}_not_renewed", extra={"subject": self.subject, "reason": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            if held:
                continue
            if not stopped.is_set():
                log.error(f"{self.log_name}_lost", extra={"subject": self.subject})
            return

    @contextmanager
    def held(self, work: str, *, renewed: bool = True) -> Generator[None]:
        """The fence held for ``work`` while the block runs, the hold renewed for as long as it runs; ``renewed=False`` for a
        block of one or two store writes, which a single hold outlasts.

        The renewals stop before the release, waiting at most one renewal interval for one in flight: a renewal landing
        after the release reads the record the release wrote, under which its take holds nothing, and renews nothing.

        Raises:
            Exception: :meth:`refused`'s error: work that excludes ``work`` holds it; the block did not run.
            botocore.exceptions.ClientError: The store refused the take.
        """
        token = self.take(work)
        if not renewed:
            try:
                yield
            finally:
                self.release(token)
            return
        stopped = threading.Event()
        keeper = threading.Thread(target=self._keep, args=(token, stopped), name=f"{self.log_name}-{self.subject}", daemon=True)
        keeper.start()
        try:
            yield
        finally:
            stopped.set()
            keeper.join(timeout=self._renewal_seconds())
            self.release(token)
