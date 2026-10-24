"""Who may change a table's history right now: a reclaim that deletes its versions, or a writer of its tags and branches ([[LH-308]]).

LANCE READS A TABLE'S REFS ONCE PER RECLAIM. ``cleanup_old_versions`` lists the referenced branches and the tags when it
starts and deletes against that listing (``rust/lance/src/dataset/cleanup.rs:472-511`` at v13.0.0), and
``lance_docs/guide.md`` § Tags and Branches promises only that a tagged version is exempt and that cleanup keeps a
branch's files. Nothing in Lance orders a ref written meanwhile with the reclaim. Measured on pylance 13.0.0 with a tag
or branch created on version 2 a few milliseconds into ``cleanup_old_versions(older_than=0)`` on a local store: the tag
named a deleted version in 20 of 60 races, and the branch could not be read in 9 of 60.

SO THE TWO NEVER OVERLAP ON ONE TABLE. Each takes the table's history fence first (:class:`TableHistory`): one record
per table on the control root, keyed by the table's uuid (ADR 0173 §8), listing who holds it now. Holders of one kind
share it, since what the fence closes is a ref written while a reclaim runs, never two reclaims or two ref writes; a
holder of the other kind refuses the take (:class:`HistoryFencedError`), and the caller answers 409 or leaves its
reclaim for the next tick. The store picks the
winner: a take is a create-if-absent or a replace under the ETag it read (:mod:`service_kit.lakehouse.records`), as a
location claim's is.

A HOLD LAPSES AFTER :data:`HOLD`, so a holder that crashed fences the table for at most that long. A reclaim still
running past its hold is no longer fenced, and its release logs ``history_fence_outlived`` at ERROR.

Every method is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from service_kit.lakehouse import records
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import record_key


log = logging.getLogger(__name__)

PREFIX: Final = "_history_fences"

#: How long one take holds the fence. Longer than any one table's reclaim the estate runs, and short enough that a
#: holder that crashed does not keep a table's refs, or its reclaim, waiting long.
HOLD: Final = timedelta(minutes=15)

#: How many read-and-write rounds a take or a release runs against holders of its own kind writing at once.
_ATTEMPTS: Final = 8


class HistoryWork(StrEnum):
    """What a holder does to the table's history."""

    #: Deletes versions: ``cleanup_old_versions`` in any of its callers.
    RECLAIM = "reclaim"
    #: Writes a tag or a branch, each of which pins a version against a reclaim.
    REFS = "refs"


class Holder(BaseModel):
    """One take of the fence."""

    token: str
    work: HistoryWork
    taken_at: datetime
    expires_at: datetime


class HistoryFence(BaseModel):
    """The fence of the table ``uuid``: its live holders, all of one kind."""

    uuid: str
    holders: list[Holder] = Field(default_factory=list)


class HistoryFencedError(Exception):
    """The table's history is held by work of the other kind, or by too many writers at once to join."""


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
        return record_key(PREFIX, "table", self.table_uuid)

    def take(self, work: HistoryWork) -> str:
        """Hold the table's history for ``work``; the token :meth:`release` ends the hold with.

        Raises:
            HistoryFencedError: Work of the other kind holds it, or the take lost every round to writers of its own kind.
            botocore.exceptions.ClientError: The store refused a read or a write.
        """
        for _ in range(_ATTEMPTS):
            now = datetime.now(UTC)
            read = records.read_json(self.control_root, self.storage_options, self.key)
            holders = _live(read[0] if read else None, now)
            if other := next((holder for holder in holders if holder.work is not work), None):
                raise HistoryFencedError(
                    f"a {other.work} holds this table's history until {other.expires_at.isoformat()}, so no {work} may start; retry once it ends"
                )
            mine = Holder(token=uuid.uuid4().hex, work=work, taken_at=now, expires_at=now + HOLD)
            fence = HistoryFence(uuid=self.table_uuid, holders=[*holders, mine]).model_dump(mode="json")
            try:
                if read is None:
                    records.create_json(self.control_root, self.storage_options, self.key, fence)
                else:
                    records.replace_json(self.control_root, self.storage_options, self.key, fence, etag=read[1])
            except (records.RecordExistsError, records.RecordChangedError):
                continue
            return mine.token
        raise HistoryFencedError(f"{_ATTEMPTS} attempts to hold this table's history for a {work} each lost to another writer; retry")

    def release(self, token: str) -> None:
        """End the hold ``token`` names. Never raises: a hold this cannot end lapses after :data:`HOLD`."""
        try:
            for _ in range(_ATTEMPTS):
                now = datetime.now(UTC)
                read = records.read_json(self.control_root, self.storage_options, self.key)
                if read is None:
                    return
                fence = HistoryFence.model_validate(read[0])
                mine = next((holder for holder in fence.holders if holder.token == token), None)
                if mine is None:
                    log.warning("history_fence_hold_gone", extra={"table_uuid": self.table_uuid})
                    return
                if mine.expires_at <= now:
                    log.error("history_fence_outlived", extra={"table_uuid": self.table_uuid, "work": mine.work, "held_since": mine.taken_at.isoformat()})
                kept = fence.model_copy(update={"holders": [holder for holder in fence.holders if holder.token != token and holder.expires_at > now]})
                try:
                    records.replace_json(self.control_root, self.storage_options, self.key, kept.model_dump(mode="json"), etag=read[1])
                except records.RecordChangedError:
                    continue
                return
            log.warning("history_fence_not_released", extra={"table_uuid": self.table_uuid, "reason": "every attempt lost to another writer"})
        except Exception as exc:  # noqa: BLE001 — a release runs in a `finally`, and must not replace the work's own outcome
            log.warning("history_fence_not_released", extra={"table_uuid": self.table_uuid, "reason": f"{type(exc).__name__}: {exc}"[:300]})

    @contextmanager
    def held(self, work: HistoryWork) -> Generator[None]:
        """The table's history held for ``work`` while the block runs.

        Raises:
            HistoryFencedError: Work of the other kind holds it; the block did not run.
            botocore.exceptions.ClientError: The store refused the take.
        """
        token = self.take(work)
        try:
            yield
        finally:
            self.release(token)
