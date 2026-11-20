"""A fixed timer of a lakehouse service: a loop in the service's own process whose every window is claimed on the store.

A service's periodic work (a relay, a reconcile, an expiry) depends on nothing that work does not need, so its timer is
neither a bus schedule nor a call from outside the process: it is a loop the process supervises
(`lakehouse.supervision`), which wakes once a window. Every replica of the service runs the loop, and the work must run
once a window however many there are, so each replica claims the window before it works: one conditional write of a
small record under the control root, carrying the ETag of the record it read, or made only if none exists. The store
lets one such write land, which is how Lance itself picks one writer of a version
(`lance_docs/lance/format/table/transaction.md` § Storage Primitives), and the replica whose write landed runs the work.

A window is named by when it began, on the STORE's clock: the read that finds the claim also carries the store's time
(`lakehouse.storage.conditional_records.read_json_at`), so no two pods' clocks are compared, and a period changed in git is due at its own
next boundary whatever length the claimed window had.

Work that raises is logged and counted, and runs again at the next window: every timer's work is level-triggered, so a
window lost is time lost and nothing else. A native panic is no Exception, so it ends the loop, and the supervisor ends
the process.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Final

from opentelemetry import metrics
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from lakehouse import control_prefixes
from lakehouse.storage import conditional_records
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)

PREFIX: Final = control_prefixes.TIMERS

#: How soon a timer whose claim could not be read or written reads it again, where its period is longer.
RETRY_SECONDS: Final = 15.0
#: The shortest a timer waits between two turns, so a boundary met exactly is never a spin.
_SHORTEST_WAIT: Final = 0.05

_meter = metrics.get_meter("lance.timers")
_turns = _meter.create_counter(
    "timer.turns",
    unit="{turn}",
    description="Turns each fixed timer's loop took in this process, by timer and by how the turn ended.",
)
_last_claimed = _meter.create_gauge(
    "timer.last_claimed",
    unit="s",
    description="When the window this process last saw claimed began, as Unix seconds on the store's clock, by timer:"
    " a timer whose value stops moving has stopped running on every replica.",
)
_period = _meter.create_gauge(
    "timer.period",
    unit="s",
    description="The period git declares for each fixed timer this process runs, which a stopped timer's age is judged by.",
)

type Work = Callable[[], Awaitable[object]]


class Outcome(StrEnum):
    """How one turn of a timer ended."""

    #: This replica claimed the window and its work returned.
    RAN = "ran"
    #: This replica claimed the window and its work raised.
    FAILED = "failed"
    #: The window is another replica's, or was claimed before this turn.
    ELSEWHERE = "elsewhere"
    #: The service holds its timers, as through a read-only window: nothing was claimed.
    HELD = "held"
    #: The claim could not be read or written, so nothing ran.
    UNREAD = "unread"


class Turn(BaseModel):
    """One turn of a timer: how it ended, the window it was about, and how long until the timer looks again."""

    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    #: When the window began on the store's clock; none where the store was never asked or did not answer.
    window: datetime | None = None
    wait: float


class _Claim(BaseModel):
    """The record a timer's windows are claimed in: the last window claimed, and who claimed it when."""

    model_config = ConfigDict(frozen=True)

    timer: str
    window: AwareDatetime
    claimed_at: AwareDatetime
    by: str


class _Asked(BaseModel):
    """What one read of the claim, and the write that followed it, settled."""

    model_config = ConfigDict(frozen=True)

    mine: bool
    window: datetime
    until_next: float
    #: The window was claimed before this turn by this same replica, which ran it.
    ran_here: bool = False


def claim_key(name: str) -> str:
    """The key of the timer ``name``'s claim record under a control root."""
    return f"{PREFIX}/{name}.json"


def _never() -> bool:
    return False


def _nothing() -> None:
    return None


class Timer:
    """One fixed timer of one replica: :meth:`run` is the loop a service keeps, :meth:`turn` one pass of it.

    Args:
        name: The timer's name, which its claim record and its metrics carry; one per kind of work on a control root.
        period: Seconds between two windows.
        work: What a claimed window runs.
        root: The control root the claim record lives under.
        storage_options: How to reach that root, read at each turn so a rotated session signs the next one.
        held: Whether the service holds its timers now; a held turn claims nothing and runs nothing.
        elsewhere: Run on a turn that finds the window another replica's: where a replica drops what only the replica
            that ran the tick knows, as a gauge the tick measured. Not run for the replica that claimed the window.
        holder: Who claims, as the record names it: the pod's host name unless given.
    """

    def __init__(
        self,
        name: str,
        *,
        period: float,
        work: Work,
        root: str,
        storage_options: Callable[[], StorageOptions],
        held: Callable[[], bool] = _never,
        elsewhere: Callable[[], None] = _nothing,
        holder: str | None = None,
    ) -> None:
        if period <= 0:
            raise ValueError(f"timer {name!r} needs a period above zero seconds, got {period}")
        self.name = name
        self._period = period
        self._work = work
        self._root = root
        self._storage_options = storage_options
        self._held = held
        self._elsewhere = elsewhere
        self._holder = holder or socket.gethostname()

    async def run(self) -> None:
        """Take turn after turn for as long as the process runs."""
        _period.set(self._period, {"timer": self.name})
        # Every outcome's series at 0 from the loop's start: one born at its first turn has no 0 before it, so an alert
        # reading the counter's increase would miss a timer's first failed turn.
        for outcome in Outcome:
            _turns.add(0, {"timer": self.name, "outcome": outcome.value})
        while True:
            turn = await self.turn()
            await asyncio.sleep(turn.wait)

    async def turn(self) -> Turn:
        """Claim the window the store's clock is in and run the work if the claim landed; how long until the next turn."""
        if self._held():
            log.info("timer_held", extra={"timer": self.name})
            return self._ended(Outcome.HELD, None, self._period)
        began = monotonic()
        try:
            asked = await asyncio.to_thread(self._ask)
        except Exception as exc:
            log.warning("timer_claim_unread", extra={"timer": self.name, "error": str(exc)[:300]})
            return self._ended(Outcome.UNREAD, None, min(self._period, RETRY_SECONDS))
        if not asked.mine:
            # A wait is measured on the monotonic clock and a window on the store's, whose front ends may stamp a second
            # apart, so the replica that ran a window can turn again inside it. What its tick measured is still the only
            # reading of that window, so only a replica that did not run it drops what it holds.
            if not asked.ran_here:
                self._elsewhere()
            return self._ended(Outcome.ELSEWHERE, asked.window, asked.until_next)
        try:
            await self._work()
        except Exception:
            log.exception("timer_work_failed", extra={"timer": self.name})
            outcome = Outcome.FAILED
        else:
            outcome = Outcome.RAN
        return self._ended(outcome, asked.window, asked.until_next - (monotonic() - began))

    def _ended(self, outcome: Outcome, window: datetime | None, wait: float) -> Turn:
        _turns.add(1, {"timer": self.name, "outcome": outcome.value})
        return Turn(outcome=outcome, window=window, wait=max(wait, _SHORTEST_WAIT))

    def _claim_in(self, record: object) -> _Claim | None:
        """The claim ``record`` holds, or none for a record that is no claim.

        The claim is the timer's own bookkeeping, so one that cannot be read as a claim says nobody holds the window:
        the replace under the ETag just read still lets one replica win, where refusing it would stop the timer on
        every replica until the record was edited by hand.
        """
        try:
            return _Claim.model_validate(record)
        except ValidationError as exc:
            log.warning("timer_claim_malformed", extra={"timer": self.name, "error": str(exc)[:300]})
            return None

    def _ask(self) -> _Asked:
        """Read the claim and, where the store's clock is in a window nobody claimed, try the one write that claims it."""
        options = self._storage_options()
        key = claim_key(self.name)
        try:
            read = conditional_records.read_json_at(self._root, options, key)
            denied = None
        except Exception as exc:
            answered_at = _denied_at(exc)
            if answered_at is None:
                raise
            # A STORE MAY ANSWER A MISSING KEY AS DENIED to an identity that may read the key and not list its bucket
            # (S3 answers 403 where a lister is answered 404), and a timer's identity is granted its one key. So a
            # denied read is taken as no claim, and the create below is the store's own answer to whether one exists.
            read, denied = conditional_records.StoreRead(stored=None, at=answered_at), exc
        window = datetime.fromtimestamp((read.at.timestamp() // self._period) * self._period, UTC)
        until_next = (window + timedelta(seconds=self._period) - read.at).total_seconds()
        held = None if read.stored is None else self._claim_in(read.stored.record)
        # A claim at or after this window is another replica's, and one a little ahead of this read's time still is: a
        # store's front ends stamp whole seconds and may differ by one, and claiming over it would run its window again.
        # Only a window more than a period ahead of the store's own time was claimed by a clock that ran away: honoured,
        # it would hold every replica off until the store's time passed it, so it is claimed over like an old one.
        if held is not None and window <= held.window <= read.at + timedelta(seconds=self._period):
            _last_claimed.set(held.window.timestamp(), {"timer": self.name})
            return _Asked(mine=False, window=held.window, until_next=until_next, ran_here=held.by == self._holder)
        claim = _Claim(timer=self.name, window=window, claimed_at=read.at, by=self._holder).model_dump(mode="json")
        try:
            if read.stored is None:
                conditional_records.create_json(self._root, options, key, claim)
            else:
                conditional_records.replace_json(self._root, options, key, claim, etag=read.stored.etag)
        except (conditional_records.RecordExistsError, conditional_records.RecordChangedError):
            if denied is not None:
                # The record exists and this identity may not read it: a fault to say, never another replica's window.
                raise denied from None
            return _Asked(mine=False, window=window, until_next=until_next)
        _last_claimed.set(window.timestamp(), {"timer": self.name})
        return _Asked(mine=True, window=window, until_next=until_next)


def _denied_at(exc: Exception) -> datetime | None:
    """The store's clock when it refused a read as forbidden; none for any other failure."""
    from botocore.exceptions import ClientError

    if not isinstance(exc, ClientError):
        return None
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if status != 403 and str(exc.response.get("Error", {}).get("Code", "")) != "AccessDenied":
        return None
    return conditional_records.store_clock(exc.response)


__all__ = ["PREFIX", "RETRY_SECONDS", "Outcome", "Timer", "Turn", "Work", "claim_key"]
