"""A fence on the control root: who holds an object now, each holder's kind of work admitted only beside work it shares
with.

Two fences are built on it: a table's history (:mod:`lakehouse.history_fences`, a reclaim against a write of its refs)
and a namespace's rows (:mod:`lakehouse.namespace_fences`, a drop against an add beneath it). Each says which kinds of
work exclude which (:meth:`Fence.excludes`), how long a hold lasts and how a refusal reads.

ONE OBJECT PER HOLDER (:class:`HolderFence`), beneath the fenced object's directory. A take writes its own, under a key
no other holder writes, then lists the directory, and yields when the listing shows a live holder of a kind its own
excludes (``specs/catalog/Fence.tla``): holders that share the fence never contend for a write, and a take is refused
only by a holder that excludes it. Measured on RustFS 1.0.3 with one shared record per object instead, each take a
replace of it under the ETag it read: 8 of 16, and 39 of 64, concurrent creates into one namespace, and 7 to 9 of 16,
and 36 to 39 of 64, concurrent tag writes on one table, answered 409 with nothing excluding them, each take having lost
its eight rounds to writers of its own kind (ADR 0233).

THE STORE PICKS THE WINNER. Every write is a create-if-absent or a replace under the ETag its writer read
(:mod:`lakehouse.records`), and the store shows a write to every listing and read that starts after it, so of two takes
that exclude each other at least one sees the other and refuses itself.

A DIRECTORY MAY CARRY MARKS (:data:`MARK_NAME`): objects named ``<kind>.<id>.json``, of a kind the fence names
(:attr:`Fence.mark_kinds`), that no take sweeps or judges, and that every take's second listing hands its holder
(:attr:`Hold.marks`), so work coming to the fenced object is seen by every holder admitted after its mark was written, at
no round trip of its own. A table's history carries the mark of each erasure release scheduled on it
(``catalog.services.erasure_release``), which only the catalog's own credential can write there.

A LISTING JUDGES ONLY THE NAMES THE FENCE DEFINES, DIRECTLY IN ITS DIRECTORY (``specs/catalog/Fence.tla`` F3). A table's
history is also written by maintenance under its table's maintain-tier vend, which may put and delete every key below
``<directory>/reclaim-``: an IAM ``*`` matches ``/``, so the vend can plant any name in a subdirectory and any body under a
reclaim's name (measured on RustFS 1.0.3: its put of ``reclaim-x/erasure-release.<id>.json`` answered 200, and a take
judging every object by its last segment handed that forged mark to the next ref door). So a take reads a holder's kind
off its KEY, a mark only where the catalog writes it, and nothing below a subdirectory; whatever else stands there, or a
body that does not parse, holds at most what its key names until it is a hold old on the store's clock, and is then
swept. No writer's object raises a take to an error, and nothing a lower tier plants outlasts a hold.

A HOLD IS KEPT FOR AS LONG AS ITS WORK RUNS ([[LH-575]]). :meth:`Fence.held` renews its hold every third of the hold
while its block runs: two renewals that fail in a row leave a third inside the hold. A holder that crashed renews nothing,
so it fences the object for at most one hold after its last renewal. A renewal that finds its hold gone, or a release
that finds it ended, logs ``<name>_lost`` or ``<name>_outlived`` at ERROR. A holder whose last step must not land once its
hold has lapsed renews it once more first (:meth:`Hold.confirm`): a renewal succeeds only while the hold stands, so one
that succeeds shows nothing excluding was admitted since the take. A renewal is written under the credential the take
was, so a holder whose credential expires can renew no further. Every hold is stamped and judged on the store's own
clock (:attr:`lakehouse.records.StoreRead.at`, the ``Date`` of the read before each write), never a pod's, so a holder's
lapse does not depend on two pods' clocks agreeing.

A HOLDER'S OWN OBJECT THAT DOES NOT PARSE IS REFUSED BY NAME when that holder renews or ends its hold
(:class:`FenceUnreadableError`): which object, and why, as the spec's InternalError, never a raw validation error. Only a
writer of that key can have made it so, and a take is never refused for it.

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
from pydantic import BaseModel, ConfigDict

from lakehouse import records, stored_objects
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: How many read-and-write rounds a renewal runs against a sweep of its own lapsed holder object.
_ATTEMPTS: Final = 8


class Holder(BaseModel):
    """One take of a fence."""

    token: str
    work: str
    taken_at: datetime
    expires_at: datetime


class FenceUnreadableError(InternalError):
    """A holder's own object does not parse as one when it renews or ends its hold: its hold cannot be judged, so it is not
    renewed (a take reads such an object as the hold its key names, until it lapses)."""


class Excluding(BaseModel):
    """A live holder a take met: its kind of work, read off its key, and when its hold ends at the latest."""

    model_config = ConfigDict(frozen=True)

    kind: str
    until: datetime


class Taken(BaseModel):
    """One take of a fence: its token, and the marks its last listing of the fence's directory showed (:data:`MARK_NAME`)."""

    model_config = ConfigDict(frozen=True)

    token: str
    #: Each mark as ``(kind, id)``.
    marks: frozenset[tuple[str, str]] = frozenset()


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
    #: The kinds of work a holder's name may carry; a name with any other is no holder.
    kinds: ClassVar[frozenset[str]] = frozenset()
    #: The kinds of mark the fence's directory may carry (:data:`MARK_NAME`); a name with any other is no mark.
    mark_kinds: ClassVar[frozenset[str]] = frozenset()

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
            botocore.exceptions.ClientError: The store refused a read or a write.
        """
        return self.taking(work).token

    def taking(self, work: str) -> Taken:
        """:meth:`take`, with the marks the take's last listing showed.

        Raises:
            Exception: :meth:`refused`'s error: work that excludes ``work`` holds it.
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
        taken = self.taking(work)
        token = taken.token
        hold = Hold(self, token, work, marks=taken.marks)
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
    """One holder's take of a fence, which its block may confirm before a step that must not land once the hold lapsed,
    and the marks the fence's directory carried when it was taken."""

    def __init__(self, fence: Fence, token: str, work: str, *, marks: frozenset[tuple[str, str]] = frozenset()) -> None:
        self._fence, self._token, self._work = fence, token, work
        #: Each mark (:data:`MARK_NAME`) the take's last listing showed, as ``(kind, id)``: written before this holder was
        #: admitted by work that excludes this holder's, or written beside it by work that shares with it.
        self.marks = marks

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


#: A holder's object name: its token, which is its kind of work and a hex id.
_HOLDER_NAME: Final = re.compile(r"(([a-z]+)-[0-9a-f]{32})\.json")

#: A mark's object name, ``<kind>.<id>.json``, which no holder's name matches.
MARK_NAME: Final = re.compile(r"([a-z-]+)\.([0-9a-f]{32})\.json")


def mark_name(kind: str, mark_id: str) -> str:
    """The object name of the mark ``(kind, mark_id)`` beneath a fence's directory.

    Raises:
        ValueError: The pair is no mark's (:data:`MARK_NAME`).
    """
    name = f"{kind}.{mark_id}.json"
    if MARK_NAME.fullmatch(name) is None:
        raise ValueError(f"{kind!r} and {mark_id!r} name no mark")
    return name


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

    def _relative(self, path: str) -> str:
        """The key of the listed object at ``path`` below :attr:`directory`.

        A listing is recursive, and a store path carries the root before the directory (``<bucket>/<key>`` on S3, the
        absolute path on a local root), so the directory's first occurrence in the path marks where its objects begin: a
        name planted below a subdirectory that repeats the directory's own key is still below that subdirectory.
        """
        marker = f"{self.directory}/"
        return path[path.find(marker) + len(marker) :]

    def _excluding(self, work: str, *, mine: str | None) -> tuple[Excluding | None, datetime, frozenset[tuple[str, str]]]:
        """A live holder of a kind ``work`` excludes, other than ``mine``, as one listing of the directory shows them, the
        store's clock at that listing, and the marks it shows; what has lapsed a whole hold ago is swept on the way.

        ONLY THE NAMES THE FENCE DEFINES, DIRECTLY IN ITS DIRECTORY, ARE JUDGED (``specs/catalog/Fence.tla`` F3). A holder is
        ``<kind>-<32 hex>.json`` with a kind of :attr:`kinds`, a mark ``<kind>.<32 hex>.json`` with a kind of
        :attr:`mark_kinds`; anything else, or anything below a subdirectory, is no holder and no mark, and goes once it has
        lapsed. A holder's kind is read off its KEY, whatever its body says, and it lapses on the store's clock: one whose
        body does not parse, names another object, or claims a later end, holds what its kind holds until it is a hold
        old. A table's maintain-tier vend may put any key below ``reclaim-``, an IAM ``*`` matching ``/`` (measured on
        RustFS 1.0.3, ADR 0233 §9): what it plants is then at most a reclaim's hold on its own table, for one hold.

        Raises:
            botocore.exceptions.ClientError: The store refused the listing or a read.
        """
        listing = stored_objects.list_under_at(f"{self.control_root.rstrip('/')}/{self.directory}", self.storage_options)
        found: Excluding | None = None
        marks: set[tuple[str, str]] = set()
        for listed in listing.objects:
            relative = self._relative(listed.path)
            name = None if "/" in relative else relative
            lapsed = listing.at - listed.modified >= self.hold()
            marked = MARK_NAME.fullmatch(name) if name is not None else None
            if marked is not None and marked.group(1) in self.mark_kinds:
                marks.add((marked.group(1), marked.group(2)))
                continue
            named = _HOLDER_NAME.fullmatch(name) if name is not None else None
            if named is None or named.group(2) not in self.kinds:
                if lapsed:
                    self._sweep_stray(relative)
                continue
            token = named.group(1)
            if token == mine:
                continue
            if lapsed:
                # Every write of a holder's object stamps it, and a hold ends at most one hold after the clock its writer
                # read before writing it: one untouched for a whole hold has lapsed, whatever its kind or its body.
                self._sweep(token)
                continue
            if found is None and self.excludes(named.group(2), work):
                found = self._live(token, kind=named.group(2), stamped=listed.modified, at=listing.at)
        return found, listing.at, frozenset(marks)

    def _live(self, token: str, *, kind: str, stamped: datetime, at: datetime) -> Excluding | None:
        """The hold the object of ``token`` stands for at ``at``, its kind its key's, or ``None`` once it is no hold: swept,
        or ended by its own body. A body that does not parse, or names another object, is the hold its key names until
        the store's clock has it a hold old (``stamped``): no writer's body can raise a take to a 500, nor outlast a hold."""
        end = stamped + self.hold()
        try:
            read = records.read_json_at(self.control_root, self.storage_options, self._key(token))
            if read.stored is None:
                return None
            parsed = self.holder_model.model_validate(read.stored.record)
        except ValueError as exc:  # bytes that are no JSON, or JSON that is no holder (pydantic's ValidationError is one)
            log.warning(f"{self.log_name}_holder_unparsed", extra={"subject": self.subject, "key": self._key(token), "reason": str(exc)[:300]})
            return Excluding(kind=kind, until=end)
        if getattr(parsed, self.stored_field, None) != self.subject:
            return Excluding(kind=kind, until=end)
        if parsed.holder is None:
            return None
        until = min(parsed.holder.expires_at, end)
        return Excluding(kind=kind, until=until) if until > at else None

    def _sweep(self, token: str) -> None:
        """Remove a lapsed holder's object: marked swept under the ETag read, then deleted, so a renewal that landed after
        the read refuses the mark and keeps its hold. One whose body does not parse is no holder's to renew, and is deleted
        as it is. Never raises: what it leaves is swept by a later take."""
        key = self._key(token)
        try:
            try:
                read, holder = self._read(key)
            except FenceUnreadableError:
                stored_objects.remove_object(self.control_root, self.storage_options, key)
                return
            # A renewal that landed since the listing ends within one hold of the store's clock now; an end claimed past
            # that was written by no take (one hold from the clock its writer read), and is swept as the listing found it.
            if read.stored is None or (holder is not None and read.at < holder.expires_at <= read.at + self.hold()):
                return
            if holder is not None:
                records.replace_json(self.control_root, self.storage_options, key, self._object(None), etag=read.stored.etag)
            stored_objects.remove_object(self.control_root, self.storage_options, key)
        except Exception as exc:
            log.warning(f"{self.log_name}_not_swept", extra={"subject": self.subject, "key": key, "reason": f"{type(exc).__name__}: {exc}"[:300]})

    def _sweep_stray(self, relative: str) -> None:
        """Remove the lapsed object ``relative`` below the directory, which is no holder and no mark the fence defines:
        whatever a writer planted there goes once it has stood a hold. Never raises."""
        key = f"{self.directory}/{relative}"
        try:
            stored_objects.remove_object(self.control_root, self.storage_options, key)
        except Exception as exc:
            log.warning(f"{self.log_name}_not_swept", extra={"subject": self.subject, "key": key, "reason": f"{type(exc).__name__}: {exc}"[:300]})

    def taking(self, work: str) -> Taken:
        """Hold the fence for ``work``: the token :meth:`release` ends the hold with, and the marks the second listing showed.

        One listing first: a live holder of a kind ``work`` excludes refuses it with nothing written. Then this take's own
        object, and a second listing: one that shows such a holder now refuses it too, its object removed. Of two takes
        that exclude each other, the one that writes second sees the first, so they never both hold the fence; both may
        refuse, and each caller retries.

        Raises:
            Exception: :meth:`refused`'s error: work that excludes ``work`` holds it.
            botocore.exceptions.ClientError: The store refused a read, a listing or a write.
        """
        other, now, _marks = self._excluding(work, mine=None)
        if other is not None:
            raise self.refused(f"a {other.kind} holds {self.what} until {other.until.isoformat()}, so no {work} may start; retry once it ends")
        token = f"{work}-{uuid.uuid4().hex}"
        mine = Holder(token=token, work=str(work), taken_at=now, expires_at=now + self.hold())
        records.create_json(self.control_root, self.storage_options, self._key(token), self._object(mine))
        try:
            other, _at, marks = self._excluding(work, mine=token)
        except Exception:
            stored_objects.remove_object(self.control_root, self.storage_options, self._key(token))
            raise
        if other is not None:
            stored_objects.remove_object(self.control_root, self.storage_options, self._key(token))
            raise self.refused(f"a {other.kind} took {self.what} beside this {work}, which yields to it; retry once it ends")
        return Taken(token=token, marks=marks)

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
