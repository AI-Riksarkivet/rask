"""A fence on the control root: who holds an object now, each holder's kind of work admitted only beside work it shares
with.

Two fences are built on it: a table's history (:mod:`lakehouse.history_fences`, a reclaim against a write of its refs)
and a namespace's rows (:mod:`lakehouse.namespace_fences`, a drop against an add beneath it). Each says which kinds of
work exclude which (:meth:`Fence.excludes`), how long a hold lasts, how a refusal reads, and how its holders are stored:

- :class:`RecordFence` keeps one record per object listing its live holders, each take or release a replace of it under
  the ETag it read. Holders that share the fence contend for that one write, so a take running beside many of its own
  kind can lose every round (:data:`_ATTEMPTS`) and be refused although nothing excludes it. The table's history is kept
  so, since a maintain-tier vend may write its table's record and no other key.
- :class:`HolderFence` keeps one object per holder beneath the object's directory. A take writes its own, under a key no
  other holder writes, then lists the directory, and yields when the listing shows a live holder of a kind its own
  excludes (``specs/catalog/Fence.tla``): holders that share the fence never contend, and a take is refused only by a
  holder that excludes it. The namespace's fence is kept so, since every create beneath a namespace takes it.

THE STORE PICKS THE WINNER. Every write is a create-if-absent or a replace under the ETag its writer read
(:mod:`lakehouse.records`), and the store shows a write to every listing and read that starts after it, so of two takes
that exclude each other at least one sees the other and refuses itself.

A HOLD IS KEPT FOR AS LONG AS ITS WORK RUNS ([[LH-575]]). :meth:`Fence.held` renews its hold every third of the hold
while its block runs: two renewals that fail in a row leave a third inside the hold. A holder that crashed renews nothing,
so it fences the object for at most one hold after its last renewal. A renewal that finds its hold gone, or a release
that finds it ended, logs ``<name>_lost`` or ``<name>_outlived`` at ERROR. A holder whose last step must not land once its
hold has lapsed renews it once more first (:meth:`Hold.confirm`): a renewal succeeds only while the hold stands, so one
that succeeds shows nothing excluding was admitted since the take. A renewal is written under the credential the take
was, so a holder whose credential expires can renew no further. Every hold is stamped and judged on the store's own
clock (:attr:`lakehouse.records.StoreRead.at`, the ``Date`` of the read before each write), never a pod's, so a holder's
lapse does not depend on two pods' clocks agreeing.

A STORED FENCE THAT DOES NOT PARSE IS REFUSED BY NAME (:class:`FenceUnreadableError`): which object, and why, as the
spec's InternalError, never a raw validation error through a door.

Every method is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, ClassVar, Final

from lance_namespace import InternalError
from pydantic import BaseModel, ConfigDict, ValidationError

from lakehouse import records, stored_objects
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: How many read-and-write rounds a record fence's take or release runs against holders of its own kind writing at once,
#: and how many a renewal runs against a sweep of its own lapsed holder object.
_ATTEMPTS: Final = 8


class Holder(BaseModel):
    """One take of a fence."""

    token: str
    work: str
    taken_at: datetime
    expires_at: datetime


class FenceUnreadableError(InternalError):
    """A fence's stored record, or a holder's object, does not parse as one: no take of the fence can judge it, so none
    is admitted until the object is repaired or removed."""


class Fence(BaseModel):
    """Where one object's fence is kept: the control root and the credential that writes there. A subclass names the
    object (:attr:`subject`), the kinds of work and which exclude which, how long a hold lasts, the refusal it raises, and
    how its holders are stored."""

    model_config = ConfigDict(frozen=True)

    control_root: str
    storage_options: StorageOptions

    #: The prefix of the events this fence logs: ``<name>_lost``, ``<name>_outlived`` and the rest.
    log_name: ClassVar[str] = "fence"
    #: The field the stored fence names its object by (``uuid`` for a table's history, ``name`` for a namespace's).
    stored_field: ClassVar[str] = "subject"

    @property
    def subject(self) -> str:
        """What the fence fences, as its stored field names it."""
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

    def take(self, work: str) -> str:
        """Hold the fence for ``work``; the token :meth:`release` ends the hold with.

        Raises:
            Exception: :meth:`refused`'s error: work that excludes ``work`` holds it.
            FenceUnreadableError: A stored holder does not parse; nothing was taken.
            botocore.exceptions.ClientError: The store refused a read or a write.
        """
        raise NotImplementedError

    def renew(self, token: str) -> bool:
        """Hold the fence for one more hold from the store's clock now, under the take ``token`` names; ``False`` when that
        take holds it no longer. A hold already past its end is not renewed: another take may have been admitted since,
        judged against the holders that stood then, and only a fresh take may hold the fence again.

        Raises:
            FenceUnreadableError: The stored hold does not parse.
            botocore.exceptions.ClientError: The store refused a read or a write.
            records.RecordChangedError: Every attempt lost to another writer of the fence.
        """
        raise NotImplementedError

    def release(self, token: str) -> None:
        """End the hold ``token`` names. Never raises: a hold this cannot end lapses after :meth:`hold`."""
        raise NotImplementedError

    def _unreadable(self, key: str, exc: Exception) -> FenceUnreadableError:
        return FenceUnreadableError(f"the fence of {self.what} at {key!r} on the control root does not parse ({type(exc).__name__}: {exc})"[:600])

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

        Raises:
            Exception: :meth:`refused`'s error: work that excludes ``work`` holds it; the block did not run.
            botocore.exceptions.ClientError: The store refused the take.
        """
        with self.hold_for(work, renewed=renewed):
            yield

    @contextmanager
    def hold_for(self, work: str, *, renewed: bool = True) -> Generator[Hold]:
        """:meth:`held`, handing the block its :class:`Hold`, for a block whose last step must not land once the hold has
        lapsed (:meth:`Hold.confirm`).

        The renewals stop before the release, waiting at most one renewal interval for one in flight: a renewal landing
        after the release finds the hold ended, and renews nothing.

        Raises:
            Exception: :meth:`refused`'s error: work that excludes ``work`` holds it; the block did not run.
            botocore.exceptions.ClientError: The store refused the take.
        """
        token = self.take(work)
        hold = Hold(self, token, work)
        if not renewed:
            try:
                yield hold
            finally:
                self.release(token)
            return
        stopped = threading.Event()
        keeper = threading.Thread(target=self._keep, args=(token, stopped), name=f"{self.log_name}-{self.subject}", daemon=True)
        keeper.start()
        try:
            yield hold
        finally:
            stopped.set()
            keeper.join(timeout=self._renewal_seconds())
            self.release(token)


class Hold:
    """One holder's take of a fence, which its block may confirm before a step that must not land once the hold lapsed."""

    def __init__(self, fence: Fence, token: str, work: str) -> None:
        self._fence, self._token, self._work = fence, token, work

    def confirm(self) -> None:
        """Renew the hold now, so a single step taken next lands inside it; refuse when it has lapsed (two renewals failed,
        or the holder stalled), since a holder of an excluding kind may have been admitted since.

        Raises:
            Exception: The fence's :meth:`Fence.refused` error: the hold lapsed.
            FenceUnreadableError: The stored hold does not parse.
            botocore.exceptions.ClientError: The store refused the renewal.
        """
        if not self._fence.renew(self._token):
            raise self._fence.refused(f"the hold of {self._fence.what} for a {self._work} lapsed before its last step; nothing more was done, retry")


class FenceRecord(BaseModel):
    """A record fence's stored record: its live holders, none of which excludes another. Each fence kind's own record
    adds the field that names its object, its :attr:`Fence.stored_field` (:attr:`RecordFence.record_model`)."""

    holders: list[Holder] = []


class RecordFence(Fence):
    """A fence kept as one record per object on the control root, at :attr:`key`, listing its live holders."""

    #: The stored record, which names the object under :attr:`Fence.stored_field`.
    record_model: ClassVar[type[FenceRecord]] = FenceRecord

    @property
    def key(self) -> str:
        """The fence record's key on the control root."""
        raise NotImplementedError

    def _holders(self, record: dict[str, Any] | None) -> list[Holder]:
        """Every holder ``record`` lists.

        Raises:
            FenceUnreadableError: It is no record of this fence: it does not parse as :attr:`record_model`, or it names
                another object.
        """
        if record is None:
            return []
        try:
            parsed = self.record_model.model_validate(record)
        except ValidationError as exc:
            raise self._unreadable(self.key, exc) from exc
        if (named := getattr(parsed, self.stored_field, None)) != self.subject:
            raise self._unreadable(self.key, ValueError(f"its {self.stored_field!r} names {named!r}, not {self.subject!r}"))
        return parsed.holders

    def _record(self, holders: list[Holder]) -> dict[str, Any]:
        return self.record_model.model_validate({self.stored_field: self.subject, "holders": holders}).model_dump(mode="json")

    def _read(self) -> records.StoreRead:
        try:
            return records.read_json_at(self.control_root, self.storage_options, self.key)
        except ValueError as exc:  # bytes that are no JSON
            raise self._unreadable(self.key, exc) from exc

    def take(self, work: str) -> str:
        """Hold the fence for ``work``; the token :meth:`release` ends the hold with.

        Raises:
            Exception: :meth:`refused`'s error: work that excludes ``work`` holds it, or the take lost every round to writers
                it shares with.
            FenceUnreadableError: The record does not parse; nothing was taken.
            botocore.exceptions.ClientError: The store refused a read or a write.
        """
        for _ in range(_ATTEMPTS):
            read = self._read()
            now = read.at
            holders = [holder for holder in self._holders(read.stored.record if read.stored else None) if holder.expires_at > now]
            if other := next((holder for holder in holders if self.excludes(holder.work, work)), None):
                raise self.refused(f"a {other.work} holds {self.what} until {other.expires_at.isoformat()}, so no {work} may start; retry once it ends")
            mine = Holder(token=uuid.uuid4().hex, work=str(work), taken_at=now, expires_at=now + self.hold())
            fence = self._record([*holders, mine])
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
        """End the hold ``token`` names. Never raises: a hold this cannot end lapses after :meth:`hold`.

        The record stays, its holders emptied: the store offers no conditional delete, and an unconditional one could
        remove a take that landed after the read. It ends with its object's other records.
        """
        try:
            for _ in range(_ATTEMPTS):
                read = self._read()
                now = read.at
                if read.stored is None:
                    return
                holders = self._holders(read.stored.record)
                mine = next((holder for holder in holders if holder.token == token), None)
                if mine is None:
                    log.warning(f"{self.log_name}_hold_gone", extra={"subject": self.subject})
                    return
                if mine.expires_at <= now:
                    log.error(f"{self.log_name}_outlived", extra={"subject": self.subject, "work": mine.work, "held_since": mine.taken_at.isoformat()})
                kept = self._record([holder for holder in holders if holder.token != token and holder.expires_at > now])
                try:
                    records.replace_json(self.control_root, self.storage_options, self.key, kept, etag=read.stored.etag)
                except records.RecordChangedError:
                    continue
                return
            log.warning(f"{self.log_name}_not_released", extra={"subject": self.subject, "reason": "every attempt lost to another writer"})
        except Exception as exc:
            log.warning(f"{self.log_name}_not_released", extra={"subject": self.subject, "reason": f"{type(exc).__name__}: {exc}"[:300]})

    def renew(self, token: str) -> bool:
        for _ in range(_ATTEMPTS):
            read = self._read()
            now = read.at
            if read.stored is None:
                return False
            holders = self._holders(read.stored.record)
            live = [holder for holder in holders if holder.expires_at > now]
            mine = next((holder for holder in live if holder.token == token), None)
            if mine is None:
                return False
            others = [holder for holder in live if holder.token != token]
            renewed = self._record([*others, mine.model_copy(update={"expires_at": now + self.hold()})])
            try:
                records.replace_json(self.control_root, self.storage_options, self.key, renewed, etag=read.stored.etag)
            except records.RecordChangedError:
                continue
            return True
        raise records.RecordChangedError(f"{_ATTEMPTS} attempts to renew a hold of {self.what} each lost to another writer")


#: A holder's object name: its token, which is its kind of work and a hex id.
_HOLDER_NAME: Final = re.compile(r"(([a-z]+)-[0-9a-f]{32})\.json")


class HolderRecord(BaseModel):
    """A holder fence's object for one holder: the hold, or ``None`` once a take swept it as lapsed. Each fence kind's own
    object adds the field that names its object, its :attr:`Fence.stored_field` (:attr:`HolderFence.holder_model`)."""

    holder: Holder | None


class HolderFence(Fence):
    """A fence kept as one object per holder beneath :attr:`directory` on the control root, each named
    ``<work>-<token>.json``, so a listing names every holder's kind of work without reading it."""

    #: A holder's stored object, which names the fenced object under :attr:`Fence.stored_field`.
    holder_model: ClassVar[type[HolderRecord]] = HolderRecord

    @property
    def directory(self) -> str:
        """The key of the directory the holders' objects live under, on the control root."""
        raise NotImplementedError

    def _key(self, token: str) -> str:
        return f"{self.directory}/{token}.json"

    def _object(self, holder: Holder | None) -> dict[str, Any]:
        return self.holder_model.model_validate({self.stored_field: self.subject, "holder": holder}).model_dump(mode="json")

    def _read(self, key: str) -> tuple[records.StoreRead, Holder | None]:
        """The object at ``key`` as the store holds it, and its hold, ``None`` where none stands there (absent or swept).

        Raises:
            FenceUnreadableError: It does not parse as a holder of this fence, or it names another object.
        """
        try:
            read = records.read_json_at(self.control_root, self.storage_options, key)
            if read.stored is None:
                return read, None
            parsed = self.holder_model.model_validate(read.stored.record)
        except ValueError as exc:  # bytes that are no JSON, or JSON that is no holder (pydantic's ValidationError is one)
            raise self._unreadable(key, exc) from exc
        if (named := getattr(parsed, self.stored_field, None)) != self.subject:
            raise self._unreadable(key, ValueError(f"its {self.stored_field!r} names {named!r}, not {self.subject!r}"))
        return read, parsed.holder

    def _excluding(self, work: str, *, mine: str | None) -> tuple[Holder | None, datetime]:
        """A live holder of a kind ``work`` excludes, other than ``mine``, as one listing of the directory shows them, and the
        store's clock at that listing; holders lapsed a whole hold ago are swept on the way.

        Raises:
            FenceUnreadableError: An excluding holder's object does not parse.
            botocore.exceptions.ClientError: The store refused the listing or a read.
        """
        listing = stored_objects.list_under_at(f"{self.control_root.rstrip('/')}/{self.directory}", self.storage_options)
        found: Holder | None = None
        for listed in listing.objects:
            named = _HOLDER_NAME.fullmatch(listed.path.rsplit("/", 1)[-1])
            if named is None or named.group(1) == mine:
                continue
            token = named.group(1)
            if listing.at - listed.modified >= self.hold():
                # Every write of a holder's object stamps it, and a hold ends at most one hold after the clock its writer
                # read before writing it: one untouched for a whole hold has lapsed, whatever its kind.
                self._sweep(token)
                continue
            if found is None and self.excludes(named.group(2), work):
                _read, holder = self._read(self._key(token))
                if holder is not None and holder.expires_at > listing.at:
                    found = holder
        return found, listing.at

    def _sweep(self, token: str) -> None:
        """Remove a lapsed holder's object: marked swept under the ETag read, then deleted, so a renewal that landed after
        the read refuses the mark and keeps its hold. Never raises: what it leaves is swept by a later take."""
        key = self._key(token)
        try:
            read, holder = self._read(key)
            if read.stored is None or (holder is not None and holder.expires_at > read.at):
                return
            if holder is not None:
                records.replace_json(self.control_root, self.storage_options, key, self._object(None), etag=read.stored.etag)
            stored_objects.remove_object(self.control_root, self.storage_options, key)
        except Exception as exc:
            log.warning(f"{self.log_name}_not_swept", extra={"subject": self.subject, "key": key, "reason": f"{type(exc).__name__}: {exc}"[:300]})

    def take(self, work: str) -> str:
        """Hold the fence for ``work``; the token :meth:`release` ends the hold with.

        One listing first: a live holder of a kind ``work`` excludes refuses it with nothing written. Then this take's own
        object, and a second listing: one that shows such a holder now refuses it too, its object removed. Of two takes
        that exclude each other, the one that writes second sees the first, so they never both hold the fence; both may
        refuse, and each caller retries.

        Raises:
            Exception: :meth:`refused`'s error: work that excludes ``work`` holds it.
            FenceUnreadableError: An excluding holder's object does not parse; nothing was taken.
            botocore.exceptions.ClientError: The store refused a read, a listing or a write.
        """
        other, now = self._excluding(work, mine=None)
        if other is not None:
            raise self.refused(f"a {other.work} holds {self.what} until {other.expires_at.isoformat()}, so no {work} may start; retry once it ends")
        token = f"{work}-{uuid.uuid4().hex}"
        mine = Holder(token=token, work=str(work), taken_at=now, expires_at=now + self.hold())
        records.create_json(self.control_root, self.storage_options, self._key(token), self._object(mine))
        try:
            other, _at = self._excluding(work, mine=token)
        except Exception:
            stored_objects.remove_object(self.control_root, self.storage_options, self._key(token))
            raise
        if other is not None:
            stored_objects.remove_object(self.control_root, self.storage_options, self._key(token))
            raise self.refused(f"a {other.work} took {self.what} beside this {work}, which yields to it; retry once it ends")
        return token

    def renew(self, token: str) -> bool:
        key = self._key(token)
        for _ in range(_ATTEMPTS):
            read, holder = self._read(key)
            if read.stored is None or holder is None or holder.expires_at <= read.at:
                return False
            renewed = self._object(holder.model_copy(update={"expires_at": read.at + self.hold()}))
            try:
                records.replace_json(self.control_root, self.storage_options, key, renewed, etag=read.stored.etag)
            except records.RecordChangedError:
                continue
            return True
        raise records.RecordChangedError(f"{_ATTEMPTS} attempts to renew a hold of {self.what} each lost to another writer")

    def release(self, token: str) -> None:
        """End the hold ``token`` names: its own object deleted, which no other holder writes. Never raises: a hold this
        cannot end lapses after :meth:`hold`, and a later take sweeps its object."""
        key = self._key(token)
        try:
            read, holder = self._read(key)
            if holder is None:
                log.warning(f"{self.log_name}_hold_gone", extra={"subject": self.subject})
            elif holder.expires_at <= read.at:
                log.error(f"{self.log_name}_outlived", extra={"subject": self.subject, "work": holder.work, "held_since": holder.taken_at.isoformat()})
        except Exception as exc:
            log.warning(f"{self.log_name}_hold_unread", extra={"subject": self.subject, "reason": f"{type(exc).__name__}: {exc}"[:300]})
        try:
            stored_objects.remove_object(self.control_root, self.storage_options, key)
        except Exception as exc:
            log.warning(f"{self.log_name}_not_released", extra={"subject": self.subject, "reason": f"{type(exc).__name__}: {exc}"[:300]})
