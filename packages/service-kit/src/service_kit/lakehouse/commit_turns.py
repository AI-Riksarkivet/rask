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

A LOST RACE IS NO LICENCE TO HOLD THE REF. A stock writer can lose on purpose, by staging a version another writer already
took, so a writer whose handed turn lapsed unused, its retry never finalizing in it, is handed no other on that ref for
:data:`COOLDOWN`; a writer whose retry finalizes releases its turn and is never held back.

ONE RECORD PER TABLE on the control root, keyed by the table's uuid (ADR 0173 §8), listing the turns held now, at most one
per ref, and the handed turns that lapsed unused while their cooldown runs. The store picks the winner: a take is a
create-if-absent or a replace under the ETag it read (:mod:`service_kit.lakehouse.records`), as a history fence's is. A
TURN LAPSES at the hold its take named, timed on the store's clock (:attr:`service_kit.lakehouse.records.StoreRead.at`), so
a holder that crashed, or a writer that never retried, stalls the ref's writers for at most that long; and a waiter waits
:data:`HOLD` at most in all, however many commits take the turn one after another, then races as it would with no turn.

Every method is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from service_kit.lakehouse import records
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import record_key


log = logging.getLogger(__name__)

PREFIX: Final = "_commit_turns"

#: How long a catalog door's commit holds the turn it took: its own loop releases it at the end, and Lance's own commit
#: retries give up after 30 s (``DEFAULT_COMMIT_RETRY_TIMEOUT``, ``rust/lance/src/io/commit.rs:88`` at v13.0.0).
HOLD: Final = timedelta(seconds=30)

#: How long a turn handed to a stock writer's retry is held. The retry follows its 409 by one rebase and one round trip
#: (measured on pylance 13.0.0: a stock writer appending alone lands 14 one-row appends a second), and a writer that never
#: retries, after a conflict Lance cannot rebase or a crash, holds the ref's other writers no longer than this.
HANDED_HOLD: Final = timedelta(seconds=5)

#: How long a writer whose handed turn lapsed unused is handed no other on that ref: one that loses races on purpose then
#: holds the ref's other writers for :data:`HANDED_HOLD` of every :data:`HANDED_HOLD` plus this, rather than throughout.
COOLDOWN: Final = timedelta(seconds=30)

#: How long a waiting finalize sleeps between two reads of the record (:func:`backoff`): 20 ms at first, so a turn of one
#: quick commit hands over at once, doubling to 250 ms, so a writer waiting out a whole hold reads the record about 130 times
#: rather than 1,500.
_POLL_SECONDS: Final = 0.02
_POLL_CAP_SECONDS: Final = 0.25

#: How many read-and-write rounds a take or a release runs against other writers of the record.
_ATTEMPTS: Final = 8


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
    """The turns of the table ``uuid``: at most one live per ref, and the handed ones that lapsed unused, while their
    cooldown runs."""

    uuid: str
    turns: list[Turn] = Field(default_factory=list)


class CommitTurnBusyError(Exception):
    """A take found another commit still holding the turn at its deadline, a handed turn found its writer cooling down, or
    every round lost to another writer of the record."""


def _kept(record: dict[str, object] | None, now: datetime) -> list[Turn]:
    """The turns a record keeps at ``now``: the live ones, and the handed ones that lapsed unused, while their cooldown runs."""
    if record is None:
        return []
    return [
        turn for turn in CommitTurns.model_validate(record).turns if turn.expires_at > now or (turn.handed_to is not None and turn.expires_at + COOLDOWN > now)
    ]


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

    def _read(self) -> tuple[records.StoreRead, list[Turn]]:
        read = records.read_json_at(self.control_root, self.storage_options, self.key)
        return read, _kept(read.stored.record if read.stored else None, read.at)

    def wait(self, ref: str | None, *, mine: str | None = None, until: float | None = None) -> bool:
        """Return once no commit but the one holding ``mine`` holds ``ref``'s turn, or at ``until`` (a ``time.monotonic``
        deadline, :data:`HOLD` from now when ``None``), whichever comes first: a waiter outlasts no single hold, however many
        commits take the turn one after another. Answer whether the record keeps a turn under ``mine`` then, held or handed
        and lapsed, which the commit releases once it lands.

        Raises:
            botocore.exceptions.ClientError: The store refused a read.
        """
        deadline = time.monotonic() + HOLD.total_seconds() if until is None else until
        attempt = 0
        while True:
            read, turns = self._read()
            if not any(turn.ref == ref and turn.expires_at > read.at and turn.token != mine for turn in turns):
                return mine is not None and any(turn.token == mine for turn in turns)
            left = deadline - time.monotonic()
            if left <= 0:
                log.warning("commit_turn_wait_outlasted", extra={"table_uuid": self.table_uuid, "ref": ref})
                return False
            time.sleep(min(backoff(attempt, first=_POLL_SECONDS, cap=_POLL_CAP_SECONDS), left))
            attempt += 1

    def _take(self, ref: str | None, *, token: str, hold: timedelta, deadline: float, handed_to: str | None) -> str:
        for _ in range(_ATTEMPTS):
            self.wait(ref, mine=token, until=deadline)
            read, turns = self._read()
            live = [turn for turn in turns if turn.ref == ref and turn.expires_at > read.at]
            if any(turn.token == token for turn in live):
                return token
            if live:
                if time.monotonic() >= deadline:
                    raise CommitTurnBusyError(f"another commit held the turn on table {self.table_uuid} past the wait")
                continue
            if handed_to is not None and any(turn.handed_to == handed_to and turn.ref == ref for turn in turns):
                raise CommitTurnBusyError(f"a turn handed to this writer on table {self.table_uuid} lapsed unused, so none is handed for {COOLDOWN}")
            mine = Turn(token=token, ref=ref, taken_at=read.at, expires_at=read.at + hold, handed_to=handed_to)
            held = CommitTurns(uuid=self.table_uuid, turns=[*turns, mine]).model_dump(mode="json")
            try:
                if read.stored is None:
                    records.create_json(self.control_root, self.storage_options, self.key, held)
                else:
                    records.replace_json(self.control_root, self.storage_options, self.key, held, etag=read.stored.etag)
            except (records.RecordExistsError, records.RecordChangedError):
                continue
            return token
        raise CommitTurnBusyError(f"{_ATTEMPTS} attempts to take a commit turn on table {self.table_uuid} each lost to another writer of its record")

    def take(self, ref: str | None) -> str:
        """Hold ``ref``'s turn for :data:`HOLD` for a catalog door's commit, once no other commit holds it, waiting :data:`HOLD`
        at most; the token :meth:`release` ends it with.

        Raises:
            CommitTurnBusyError: Another commit still held the turn after the wait, or every round lost to another writer of
                the record.
            botocore.exceptions.ClientError: The store refused a read or a write.
        """
        return self._take(ref, token=uuid.uuid4().hex, hold=HOLD, deadline=time.monotonic() + HOLD.total_seconds(), handed_to=None)

    def hand(self, ref: str | None, writer: Writer) -> None:
        """Hand ``ref``'s turn to ``writer`` for :data:`HANDED_HOLD`, unless another commit holds it now; a turn ``writer``
        holds already is kept as it is.

        Raises:
            CommitTurnBusyError: Another commit holds the turn, a turn handed to ``writer``'s caller lapsed unused within
                :data:`COOLDOWN`, or every round lost to another writer of the record.
            botocore.exceptions.ClientError: The store refused a read or a write.
        """
        self._take(ref, token=writer.token, hold=HANDED_HOLD, deadline=time.monotonic(), handed_to=writer.caller)

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
