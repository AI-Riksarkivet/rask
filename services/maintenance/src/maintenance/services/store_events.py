"""The store's change events (ADR 0198 §3): a write under a table holds that table's pass a delay out.

The store publishes every object write of a rask bucket on the bus subject ``store.events``: its operator configures
each bucket to (``objectStore.events``; the development store's fixture does it for its own, ``deploy/fixtures/rustfs``),
and the worker reads them off STORE_EVENTS (:meth:`maintenance.services.queue.Worker.run_store_events`). An event names a
key, never what it means to the table. The pass it holds reads every file the table's commits recorded again
(:mod:`maintenance.services.committed_files`), so a file overwritten in place is reported within the follow-up's delay
rather than at the table's next recheck, which stays the backstop for an event that never arrives. A delete is not acted
on: a recorded file deleted while a retained version names it breaks every read of the table at once, and the table's
daily pass names it, while maintenance's own cleanup deletes would otherwise hold a second pass after every pass.

A key's table is a root its key passes through just before a directory a vended writer may put under
(``lakehouse.table_layout.WRITER_PUT_GLOBS``: ``data``, ``_versions``, ``_transactions``, ``_deletions``, ``_indices``, and
``tree`` for a branch's own), the only places a recorded file can be overwritten; the rest of a table's layout
(``_blobs``, ``_refs``, ``_bt``, ``lance_docs/lance/format/table/layout.md``) takes no vended write. The claim on that
root names the table, and the roots are tried shallowest first, so a warehouse prefix that happens to be named like one
of those directories hides nothing. A key with no such directory, as every record on the control root is, reads nothing.
Each replica keeps what it found briefly: a commit writes several objects under one table, and its events then cost one
claim read and one hold, not one each.

Measured on RustFS 1.0.3 with nats-server 2.15.2 (2026-11-09): one message per write, overwrite in place and delete, each
with one record, its bucket at ``data.s3.bucket.name``, its key URL-encoded at ``data.s3.object.key`` and its event at
``data.eventName``; writes made while the bus was down were published about 5 s after it returned. A store that sends
S3's own record shape names them at ``s3.bucket.name``, ``s3.object.key`` and ``eventName``, which are read as well.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Final
from urllib.parse import unquote_plus

from botocore.exceptions import BotoCoreError, ClientError
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lakehouse import location_claims, table_layout
from lakehouse.governed.store_credential import StoreCredentialUnavailableError
from maintenance.core.config import MaintenanceSettings
from maintenance.services.naming import claim_store, claimed_location
from maintenance.services.queue import Task, TaskQueue


log = logging.getLogger(__name__)

#: The directories a vended writer may put under, relative to a table's root; a key passes through its table's root just
#: before one of them.
_WRITABLE_DIRECTORIES: Final = frozenset(glob.split("/", 1)[0] for glob in (*table_layout.WRITER_PUT_GLOBS, table_layout.BRANCH_STAGING_GLOB))
#: The event names of a write, the only events acted on.
_WRITE: Final = "s3:ObjectCreated:"
#: How long a replica keeps a root's table, or that no table holds the root, and how many roots it keeps.
_CLAIM_SECONDS: Final = 300.0
_NO_CLAIM_SECONDS: Final = 30.0
_REMEMBERED: Final = 4096


class StoredKey(BaseModel):
    """One object a store event names, and whether the event wrote it."""

    model_config = ConfigDict(frozen=True)

    bucket: str
    key: str
    written: bool


class _Bucket(BaseModel):
    name: str = Field(min_length=1)


class _Object(BaseModel):
    key: str = Field(min_length=1)


class _Entity(BaseModel):
    bucket: _Bucket
    stored: _Object = Field(validation_alias="object")


class _Record(BaseModel):
    """One object a store event names: its event's name, and its bucket and key."""

    event_name: str = Field(default="", validation_alias="eventName")
    s3: _Entity

    @model_validator(mode="before")
    @classmethod
    def _unwrapped(cls, record: object) -> object:
        """RustFS sends each record under ``data``; S3 sends it bare."""
        if isinstance(record, dict) and isinstance(record.get("data"), dict):
            return record["data"]
        return record


class _StoreEvent(BaseModel):
    records: list[_Record] = Field(validation_alias="Records")


def stored_keys(body: bytes) -> list[StoredKey]:
    """Every object the store event ``body`` names, its key decoded.

    Raises:
        ValueError: ``body`` is no store event: not JSON, or a record that names no bucket and key (pydantic's
            ``ValidationError`` is one).
    """
    event = _StoreEvent.model_validate_json(body)
    return [
        StoredKey(bucket=record.s3.bucket.name, key=unquote_plus(record.s3.stored.key), written=record.event_name.startswith(_WRITE))
        for record in event.records
    ]


def table_roots(stored: StoredKey) -> list[str]:
    """Every root, as a store key (``<bucket>/<prefix>``), the object ``stored`` could be a table's file under, shallowest
    first: each prefix of its key that a Lance directory follows."""
    segments = stored.key.split("/")
    return ["/".join([stored.bucket, *segments[:at]]) for at in range(1, len(segments)) if segments[at] in _WRITABLE_DIRECTORIES]


class StoreEvents:
    """What one replica does with the store's events: hold the pass of each table one writes under."""

    def __init__(self, settings: MaintenanceSettings, queue: TaskQueue) -> None:
        self._settings = settings
        self._queue = queue
        #: Each root asked about lately: until when the answer stands, and the table found there, or none.
        self._found: dict[str, tuple[float, Task | None]] = {}
        #: Each table whose follow-up this replica holds, until it fires: until then that follow-up covers every write.
        self._holding: dict[str, float] = {}

    async def hold_passes(self, body: bytes) -> int:
        """Hold the pass of each table the store event ``body`` writes an object under, the commit delay out; how many it
        held. A table whose follow-up is held already is covered by it.

        Raises:
            ValueError: ``body`` is no store event.
            OSError: The store refused, or could not be reached for, a claim's read.
            QueueUnavailableError: The bus did not take a follow-up.
        """
        tables: dict[str, Task] = {}
        for stored in stored_keys(body):
            if stored.written and (task := await self._table_of(stored)) is not None:
                tables[task.uuid] = task
        now, held = monotonic(), 0
        delay = self._settings.commit_pass_delay_seconds
        for task in tables.values():
            if self._holding.get(task.uuid, 0.0) > now:
                continue
            if await self._queue.follow_up(task, datetime.now(UTC) + timedelta(seconds=delay)):
                self._remember(self._holding, task.uuid, now + delay)
                held += 1
        return held

    async def _table_of(self, stored: StoredKey) -> Task | None:
        for root in table_roots(stored):
            now = monotonic()
            until, task = self._found.get(root, (0.0, None))
            if until <= now:
                task = await self._claimed(root)
                self._remember(self._found, root, (now + (_CLAIM_SECONDS if task is not None else _NO_CLAIM_SECONDS), task))
            if task is not None:
                return task
        return None

    async def _claimed(self, root: str) -> Task | None:
        """The table whose claim holds the store key ``root``, or ``None`` when no table holds it.

        Raises:
            OSError: The store refused, or could not be reached for, the claim's read.
        """
        try:
            # The claim store is built at each read: it signs with the session in force now, and the one held when the
            # replica started expires within the hour.
            claim = await run_in_threadpool(location_claims.holder, claim_store(self._settings), f"s3://{root}")
        except ValueError as exc:
            log.warning("maintenance_store_event_claim_unreadable", extra={"root": root, "error": str(exc)[:300]})
            return None
        except (ClientError, BotoCoreError, StoreCredentialUnavailableError) as exc:
            raise OSError(f"the claim on {root} could not be read: {exc}") from exc
        if claim is None or claim.uuid is None:
            return None
        return Task(kind="table", uuid=claim.uuid, location=claimed_location(self._settings.resolved_control_root, claim))

    @staticmethod
    def _remember[V](memory: dict[str, V], key: str, value: V) -> None:
        memory.pop(key, None)
        memory[key] = value
        while len(memory) > _REMEMBERED:
            memory.pop(next(iter(memory)))
