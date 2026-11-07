"""Which commit a table's ref takes next once a commit has lost its race to other writers ([[LH-259]]).

THE CATALOG FINALIZES EXACTLY THE VERSION A COMMIT FOLLOWS. Under managed versioning every version of a table is a
put-if-not-exists of the version after the one its writer planned or staged on (``lance_docs/ns_catalog/spec.yaml``
CreateTableVersion), so a writer that commits more often than another plans and finalizes wins every version, and the
slower one plans again until its attempts run out. Measured against a real ``dir`` root with the catalog's ``/insert``
appending in a tight loop: all ten plans of a ``cast(NULL)`` add lost in 6 of 6 rounds, an update and a delete answered
409 after ten, and a stock writer committing through ``version/create`` landed 0 appends beside 639. A schema change is
the sharpest case, since Lance cannot rebase a ``Merge`` over an Append (``lance_docs/lance/format/table/transaction.md``
§ Merge Compatibility) and answers it by making schema changes while no other write happens
(``lance_docs/lance/guide/data_evolution.md``).

A TURN IS ONE REF'S, FOR ONE COMMIT. A catalog door's commit that lost its race takes the turn of the ref it commits on
and holds it for its remaining attempts (:meth:`TableTurns.held`); a stock writer that lost its race is handed the ref's
turn (:meth:`TableTurns.hand`) under its verified caller and its transaction's uuid, which pylance keeps when it stages the
commit again on the version that won (measured on pylance 13.0.0), so its retry finalizes next. Every other finalize of
that ref waits while a turn is held (:meth:`TableTurns.wait`), so the holder meets only the writers already past their
wait. A writer waits rather than being refused: measured on pylance 13.0.0, a stock writer whose finalize answered 409 with
no version at its target failed its commit as of unknown outcome.

A HANDED TURN HOLDS THE CATALOG'S OWN WRITERS, NOT A STOCK WRITER'S PEERS. The catalog's doors and its ``/insert`` commit
in-process, far faster than a stock writer's round trip, so they wait out a turn handed to one; other stock writers race it
as Lance's own backoff spreads them (``handed`` on :meth:`TableTurns.wait`). Measured 2026-10-31 with 16 stock writers on
one table at the estate's round trip: waiting on each other's handed turns they landed 4 of 32 appends, racing them 10 of
32, as many as with no turn. A handed turn that lapses unused simply lapses, as a Kubernetes Lease or an etcd lease does,
and nothing is held against the identity of its writer, which a pipeline's workers share.

ONE RECORD PER TABLE on the control root, keyed by the table's uuid (ADR 0173 §8), listing the turns held now, at most one
per ref. The store picks the winner: a take is a create-if-absent or a replace under the ETag it read
(:mod:`lakehouse.records`), as a history fence's is. A TURN LAPSES at the hold its take named, timed on the
store's clock (:attr:`lakehouse.records.StoreRead.at`), so a holder that crashed, or a writer that never
retried, stalls the ref's writers for at most that long; and a waiter waits :data:`HOLD` at most in all, however many
commits take the turn one after another, then races as it would with no turn.

A WAITER HOLDS NO REQUEST THREAD WHERE IT CAN AVOID IT. A door that awaits :meth:`TableTurns.wait_in_loop` holds none while
it waits; one that waits from a thread (:meth:`TableTurns.wait`) holds its thread while it sleeps, so at most
:data:`THREAD_WAITERS` sleep at once in a process and a wait past them is refused, as a bulkhead refuses. A table's waiters
in one process share one read of its record (:data:`_SHARED_READ_SECONDS`), so a waiter costs the same however many wait.
Only waiting takes that shared view; whether the record keeps a writer's own turn, which decides its release, is read from
the store (:meth:`TableTurns.keeps`), as Lance decides a commit it lost the answer to by reading the version back.

Every method but :meth:`TableTurns.wait_in_loop` is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from lakehouse import records
from lakehouse.record_store import record_key
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

PREFIX: Final = "_commit_turns"

#: How long a turn is held: a catalog door's commit holds it for its remaining attempts and releases it at the end, and a
#: stock writer is handed it for its retries. Lance's own commit retries give up after 30 s (``DEFAULT_COMMIT_RETRY_TIMEOUT``,
#: ``rust/lance/src/io/commit.rs:88`` at v13.0.0), and each follows its conflict by a random 0 to 3 slots of 1.1 times the
#: first attempt's duration, then 0 to 7 (``SlotBackoff``, ``rust/lance-core/src/utils/backoff.rs``), so only this budget
#: covers every retry a writer makes: measured on the estate 2026-10-31, a stock writer's retries came 6.2, 6.7 and 11.7 s
#: after its 409s, and a turn held 5 s lapsed before each.
HOLD: Final = timedelta(seconds=30)

#: How many threads of one process may sleep waiting for a turn at once. A sleeping waiter holds one of the request threads
#: the process serves its sync doors with (anyio's default limiter has 40, ``anyio/_backends/_asyncio.py:3097`` at 4.14.2),
#: so a wait past these is refused rather than queued, as a bulkhead refuses when full (resilience4j's
#: ``SemaphoreBulkhead``, Polly's ``ConcurrencyLimiter``): measured 2026-10-31, 20 waiting threads left a describe of
#: another table answering in 0.02 s, and 50 made it wait 26.8 s.
THREAD_WAITERS: Final = 20
_THREAD_WAITER_SEATS: Final = threading.BoundedSemaphore(THREAD_WAITERS)

#: How long a waiting finalize sleeps between two reads of the record (:func:`backoff`): 20 ms at first, so a turn of one
#: quick commit hands over at once, doubling to 250 ms, so a writer waiting out a whole hold reads the record about 130 times
#: rather than 1,500.
_POLL_SECONDS: Final = 0.02
_POLL_CAP_SECONDS: Final = 0.25

#: How many read-and-write rounds a take or a release runs against other writers of the record.
_ATTEMPTS: Final = 8

#: How long one read of a table's record answers every waiter of that table in the process (:meth:`TableTurns.poll`),
#: counted from when the store answered it. Read per waiter, the record's reads and the catalog's CPU grew with the
#: waiters: measured on the estate 2026-10-31, 53 writers waiting on one held turn pegged the catalog's 1-CPU limit, and a
#: describe of another table slowed from 0.07 s to 1-2 s. An object store wakes no waiter, as etcd's lock wakes each of
#: its waiters through a watch (etcd ``client/v3/concurrency/mutex.go:89``), so the process keeps one view of the record
#: for all of them, as a client-go shared informer keeps one cache for every consumer in its process
#: (``tools/cache/shared_informer.go:48-70``).
_SHARED_READ_SECONDS: Final = 0.1

#: The most tables the process keeps a shared read for at once; one more starts them all over.
_SHARED_READ_TABLES: Final = 1024


def backoff(attempt: int, *, first: float, cap: float) -> float:
    """The seconds to wait before attempt ``attempt + 1`` (counted from 0): ``first``, doubled each attempt up to ``cap``,
    with up to half of it again at random, so writers waiting on one record read or write it apart rather than in step."""
    wait = min(first * 2**attempt, cap)
    return wait + random.uniform(0, wait / 2)  # noqa: S311 — spreads racing writers, guards nothing


class Writer(BaseModel):
    """A stock writer a turn is handed to: its verified caller (``""`` with authentication off) and the uuid of the
    transaction it staged, which pylance keeps when it stages the commit again (measured on pylance 13.0.0)."""

    model_config = ConfigDict(frozen=True)

    caller: str
    transaction: str = Field(min_length=1)

    @property
    def token(self) -> str:
        """The token its turn is held under: no other caller's commit, nor its own other commits, holds it."""
        return f"{self.caller}#{self.transaction}"


class Turn(BaseModel):
    """One ref's turn: the commit holding it, and when it lapses."""

    token: str
    #: The branch it is held on, as Lance records it: ``None`` for main.
    ref: str | None
    taken_at: datetime
    expires_at: datetime
    #: The caller a stock writer's turn was handed to; ``None`` for a catalog door's own.
    handed_to: str | None = None


class CommitTurns(BaseModel):
    """The turns of the table ``uuid``: at most one live per ref."""

    uuid: str
    turns: list[Turn] = Field(default_factory=list)


class CommitTurnBusyError(Exception):
    """A take found another commit still holding the turn at its deadline, a wait found :data:`THREAD_WAITERS` threads of
    the process already sleeping, or every round lost to another writer of the record."""


def _kept(record: dict[str, object] | None, now: datetime) -> list[Turn]:
    """The turns a record keeps at ``now``: the live ones."""
    if record is None:
        return []
    return [turn for turn in CommitTurns.model_validate(record).turns if turn.expires_at > now]


def _holds(turns: list[Turn], ref: str | None, *, mine: str | None, handed: bool) -> bool:
    """Whether a commit but the one holding ``mine`` holds ``ref``'s turn among ``turns``, counting a turn handed to a stock
    writer only with ``handed`` (a stock writer's peers race it)."""
    return any(turn.ref == ref and turn.token != mine and (handed or turn.handed_to is None) for turn in turns)


class _SharedRead(BaseModel):
    """Which answer a waiter may take: a read of one table's record, on one store, signed as one identity. A waiter
    reaching another store or signing as another identity is answered only by a read of its own, so the store judges every
    credential a record is read under."""

    model_config = ConfigDict(frozen=True)

    control_root: str
    table_uuid: str
    #: The store's non-credential options (endpoint, region, path style) the read is made with.
    options: tuple[tuple[str, str], ...]
    #: The access key id the read is signed with, the credential's identity as the store names it; ``None`` for the
    #: ambient chain. Never the secret, which no key holds.
    signer: str | None


class _Answer(BaseModel):
    """What one read of a table's record found, and when the store answered it (``time.monotonic``)."""

    model_config = ConfigDict(frozen=True)

    turns: list[Turn]
    answered_at: float


class _SharedReads:
    """Each table's live turns as the last read of its record found them, for every waiter of the table in the process,
    in the shape of :class:`lakehouse.governed.secret_cache.SecretCache`: one waiter reads, the waiters queued behind
    its read take its answer, and so does every waiter until :data:`_SHARED_READ_SECONDS` after the store answered. The
    process's write of a table's record clears that table's view, and a read of it in flight across the write keeps
    nothing, so no waiter of the process acts on a view from before the process's own write, and a write of another
    table's record touches none of it; another process's write is seen once this view is renewed. Only waiting takes it
    (:meth:`TableTurns.poll`).

    Thread-safe: one lock per read orders its loads; a load's answer is kept, and a clearing counted, under the guard that
    hands out the locks; a held answer is taken without a lock, since CPython reads a dict key atomically. Past
    :data:`_SHARED_READ_TABLES` the locks start over, and two loads of one read may then run at once.
    """

    def __init__(self) -> None:
        self._held: dict[_SharedRead, _Answer] = {}
        self._locks: dict[_SharedRead, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        #: How often each read's view was cleared (:meth:`forget`) since the locks last started over, counted only for a
        #: read that has a lock, the only kind a load can be in flight for; a load during which its count moved keeps no
        #: answer.
        self._clearings: dict[_SharedRead, int] = {}
        #: How often the locks started over: a load in flight across a start keeps no answer either.
        self._starts = 0

    def get(self, read: _SharedRead, load: Callable[[], list[Turn]]) -> list[Turn]:
        """``read``'s live turns: the held ones while under :data:`_SHARED_READ_SECONDS` since the store answered them,
        else what ``load`` returns.

        Blocking: a caller that misses waits for the one load in flight for ``read``, and takes its answer.

        Raises:
            Exception: Whatever ``load`` raises. Nothing is held then.
        """
        if (held := self.peek(read)) is not None:
            return held
        with self._lock_for(read):
            if (held := self.peek(read)) is not None:
                return held
            with self._locks_guard:
                before = self._cleared(read)
            turns = load()
            with self._locks_guard:
                if self._cleared(read) == before:
                    self._held[read] = _Answer(turns=turns, answered_at=time.monotonic())
            return turns

    def forget(self, read: _SharedRead) -> None:
        """Drop what is held for ``read``, once the process wrote its record or found the store holding a turn the view did
        not show: its next waiter reads the record again, and a load of it in flight keeps nothing."""
        with self._locks_guard:
            if read in self._locks:
                self._clearings[read] = self._clearings.get(read, 0) + 1
            self._held.pop(read, None)

    def _cleared(self, read: _SharedRead) -> tuple[int, int]:
        """Where ``read``'s clearings stand: the locks' starts and its own count. Called under the guard."""
        return self._starts, self._clearings.get(read, 0)

    def peek(self, read: _SharedRead) -> list[Turn] | None:
        """``read``'s held turns while under :data:`_SHARED_READ_SECONDS` since the store answered them, ``None`` otherwise.
        Never reads the store and takes no lock, so the event loop calls it itself."""
        held = self._held.get(read)
        if held is None or time.monotonic() - held.answered_at >= _SHARED_READ_SECONDS:
            return None
        return held.turns

    def _lock_for(self, read: _SharedRead) -> threading.Lock:
        with self._locks_guard:
            if read not in self._locks and len(self._locks) >= _SHARED_READ_TABLES:
                self._locks.clear()
                self._held.clear()
                self._clearings.clear()
                self._starts += 1
            return self._locks.setdefault(read, threading.Lock())


#: This process's shared reads of the tables' turn records.
_SHARED_READS: Final = _SharedReads()


class TableTurns(BaseModel):
    """Where one table's commit turns are kept: the control root, the credential that writes there, and the table's uuid."""

    model_config = ConfigDict(frozen=True)

    control_root: str
    storage_options: StorageOptions
    table_uuid: str

    @property
    def key(self) -> str:
        """The record's key on the control root."""
        return record_key(PREFIX, "table", self.table_uuid)

    @property
    def _shared(self) -> _SharedRead:
        """The shared read this table's waiters in the process take their answers from."""
        options = self.storage_options
        return _SharedRead(
            control_root=self.control_root,
            table_uuid=self.table_uuid,
            options=tuple(sorted(options.options.items())),
            signer=options.key_pair.access_key_id if options.key_pair is not None else None,
        )

    def _read(self) -> tuple[records.StoreRead, list[Turn]]:
        read = records.read_json_at(self.control_root, self.storage_options, self.key)
        return read, _kept(read.stored.record if read.stored else None, read.at)

    def _live_turns(self) -> list[Turn]:
        return self._read()[1]

    def poll(self, ref: str | None, *, mine: str | None = None, handed: bool = True) -> bool:
        """Whether a commit but the one holding ``mine`` holds ``ref``'s turn now, as the table's shared read finds it
        (:data:`_SHARED_READS`), counting a turn handed to a stock writer only with ``handed`` (a stock writer's peers race
        it).

        Raises:
            botocore.exceptions.ClientError: The store refused a read.
        """
        return _holds(_SHARED_READS.get(self._shared, self._live_turns), ref, mine=mine, handed=handed)

    def keeps(self, token: str) -> bool:
        """Whether the record keeps a live turn under ``token`` now, read from the store and never from the shared read: a
        writer decides by it whether to release the turn it was handed, and an answer from before the hand would leave that
        turn held to its lapse, with the catalog's own writers of the ref waiting on it.

        Raises:
            botocore.exceptions.ClientError: The store refused the read.
        """
        return any(turn.token == token for turn in self._live_turns())

    def wait(self, ref: str | None, *, mine: str | None = None, until: float | None = None, handed: bool = True) -> None:
        """Return once no commit but the one holding ``mine`` holds ``ref``'s turn (:meth:`poll`), or at ``until`` (a
        ``time.monotonic`` deadline, :data:`HOLD` from now when ``None``), whichever comes first: a waiter outlasts no
        single hold, however many commits take the turn one after another. The calling thread sleeps between reads in one
        of :data:`THREAD_WAITERS` seats.

        Raises:
            CommitTurnBusyError: The wait would sleep while every seat is taken.
            botocore.exceptions.ClientError: The store refused a read.
        """
        deadline = time.monotonic() + HOLD.total_seconds() if until is None else until
        attempt, seated = 0, False
        try:
            while self.poll(ref, mine=mine, handed=handed):
                left = deadline - time.monotonic()
                if left <= 0:
                    log.warning("commit_turn_wait_outlasted", extra={"table_uuid": self.table_uuid, "ref": ref})
                    return
                if not seated and not _THREAD_WAITER_SEATS.acquire(blocking=False):
                    raise CommitTurnBusyError(f"{THREAD_WAITERS} threads of this process already wait for a commit turn")
                seated = True
                time.sleep(min(backoff(attempt, first=_POLL_SECONDS, cap=_POLL_CAP_SECONDS), left))
                attempt += 1
        finally:
            if seated:
                _THREAD_WAITER_SEATS.release()

    async def wait_in_loop(self, ref: str | None, *, mine: str | None = None, handed: bool = True) -> bool:
        """:meth:`wait` from the event loop, holding no request thread: a fresh shared answer is taken in the loop itself
        (:meth:`_SharedReads.peek`, no I/O and no lock), a read of the record runs on the loop's own executor for as long as
        the read takes, and the pauses between them are the loop's, so a door awaiting this costs the same however many
        writers wait, and stops waiting when its request is cancelled. Then answer whether the record keeps a turn under
        ``mine`` (:meth:`keeps`), which the commit releases once it lands.

        Measured 2026-11-01 on a local control root: 53 waiters on one held turn cost the process 32-35 millicores of CPU
        this way, against 60-62 with each poll sent to the executor.

        Raises:
            botocore.exceptions.ClientError: The store refused a read.
        """
        read = self._shared
        deadline = time.monotonic() + HOLD.total_seconds()
        attempt = 0
        while True:
            turns = _SHARED_READS.peek(read)
            if turns is None:
                turns = await asyncio.to_thread(_SHARED_READS.get, read, self._live_turns)
            if not _holds(turns, ref, mine=mine, handed=handed):
                break
            left = deadline - time.monotonic()
            if left <= 0:
                log.warning("commit_turn_wait_outlasted", extra={"table_uuid": self.table_uuid, "ref": ref})
                break
            await asyncio.sleep(min(backoff(attempt, first=_POLL_SECONDS, cap=_POLL_CAP_SECONDS), left))
            attempt += 1
        return mine is not None and await asyncio.to_thread(self.keeps, mine)

    def _take(self, ref: str | None, *, token: str, hold: timedelta, deadline: float, handed_to: str | None) -> str:
        for _ in range(_ATTEMPTS):
            if time.monotonic() < deadline:
                # A hand's deadline is now: it is handed the turn only when no other commit holds it, and waits for none.
                self.wait(ref, mine=token, until=deadline)
            read, turns = self._read()
            live = [turn for turn in turns if turn.ref == ref and turn.expires_at > read.at]
            if any(turn.token == token for turn in live):
                return token
            if live:
                if time.monotonic() >= deadline:
                    raise CommitTurnBusyError(f"another commit holds the turn on table {self.table_uuid}")
                # The store holds a turn the view let the wait past (another replica's, or one read under another
                # signer): the next wait reads the record itself, rather than spending this take's rounds on that view.
                _SHARED_READS.forget(self._shared)
                continue
            mine = Turn(token=token, ref=ref, taken_at=read.at, expires_at=read.at + hold, handed_to=handed_to)
            held = CommitTurns(uuid=self.table_uuid, turns=[*turns, mine]).model_dump(mode="json")
            try:
                if read.stored is None:
                    records.create_json(self.control_root, self.storage_options, self.key, held)
                else:
                    records.replace_json(self.control_root, self.storage_options, self.key, held, etag=read.stored.etag)
            except (records.RecordExistsError, records.RecordChangedError):
                continue
            _SHARED_READS.forget(self._shared)
            return token
        raise CommitTurnBusyError(f"{_ATTEMPTS} attempts to take a commit turn on table {self.table_uuid} each lost to another writer of its record")

    def take(self, ref: str | None) -> str:
        """Hold ``ref``'s turn for :data:`HOLD` for a catalog door's commit, once no other commit holds it, waiting :data:`HOLD`
        at most; the token :meth:`release` ends it with.

        Raises:
            CommitTurnBusyError: Another commit still held the turn after the wait, the wait found every seat taken, or
                every round lost to another writer of the record.
            botocore.exceptions.ClientError: The store refused a read or a write.
        """
        return self._take(ref, token=uuid.uuid4().hex, hold=HOLD, deadline=time.monotonic() + HOLD.total_seconds(), handed_to=None)

    def hand(self, ref: str | None, writer: Writer) -> None:
        """Hand ``ref``'s turn to ``writer`` for :data:`HOLD`, unless another commit holds it now; a turn ``writer`` holds
        already is kept as it is.

        Raises:
            CommitTurnBusyError: Another commit holds the turn, or every round lost to another writer of the record.
            botocore.exceptions.ClientError: The store refused a read or a write.
        """
        self._take(ref, token=writer.token, hold=HOLD, deadline=time.monotonic(), handed_to=writer.caller)

    def release(self, token: str) -> None:
        """End the turn ``token`` names, held or handed and lapsed. Never raises: a turn this cannot end lapses at the hold
        its take named."""
        try:
            for _ in range(_ATTEMPTS):
                read, turns = self._read()
                if read.stored is None or not any(turn.token == token for turn in turns):
                    log.warning("commit_turn_gone", extra={"table_uuid": self.table_uuid})
                    return
                kept = CommitTurns(uuid=self.table_uuid, turns=[turn for turn in turns if turn.token != token]).model_dump(mode="json")
                try:
                    records.replace_json(self.control_root, self.storage_options, self.key, kept, etag=read.stored.etag)
                except records.RecordChangedError:
                    continue
                _SHARED_READS.forget(self._shared)
                return
            log.warning("commit_turn_not_released", extra={"table_uuid": self.table_uuid, "reason": "every attempt lost to another writer"})
        except Exception as exc:  # noqa: BLE001 — a release runs in a `finally`, and must not replace the commit's own outcome
            log.warning("commit_turn_not_released", extra={"table_uuid": self.table_uuid, "reason": f"{type(exc).__name__}: {exc}"[:300]})

    @contextmanager
    def held(self, ref: str | None) -> Generator[str]:
        """``ref``'s turn held while the block runs, its token handed to the block.

        Raises:
            CommitTurnBusyError: Another commit still held the turn after the wait, or every round of the take lost to
                another writer of the record; the block did not run.
            botocore.exceptions.ClientError: The store refused the take.
        """
        token = self.take(ref)
        try:
            yield token
        finally:
            self.release(token)
