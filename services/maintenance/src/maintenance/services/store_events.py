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

import json
import logging
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Final
from urllib.parse import unquote_plus

from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict

from lakehouse import location_claims, table_layout
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


def stored_keys(body: bytes) -> list[StoredKey]:
    """Every object the store event ``body`` names, its key decoded.

    Raises:
        ValueError: ``body`` is no store event: not JSON, or naming no ``Records``.
    """
    try:
        event = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"a store event is JSON: {exc}") from exc
    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list):
        raise ValueError("a store event names its objects under `Records`")
    return [found for record in records if (found := _stored_key(record)) is not None]


def _stored_key(record: Any) -> StoredKey | None:
    if not isinstance(record, dict):
        return None
    data = record.get("data")
    body = data if isinstance(data, dict) else record
    s3, name = body.get("s3"), body.get("eventName")
    if not isinstance(s3, dict):
        return None
    bucket, key = (s3.get("bucket") or {}).get("name"), (s3.get("object") or {}).get("key")
    if not (isinstance(bucket, str) and isinstance(key, str) and bucket and key):
        return None
    return StoredKey(bucket=bucket, key=unquote_plus(key), written=isinstance(name, str) and name.startswith(_WRITE))


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
        self._claims = claim_store(settings)
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
        try:
            claim = await run_in_threadpool(location_claims.holder, self._claims, f"s3://{root}")
        except ValueError as exc:
            log.warning("maintenance_store_event_claim_unreadable", extra={"root": root, "error": str(exc)[:300]})
            return None
        if claim is None or claim.uuid is None:
            return None
        return Task(kind="table", uuid=claim.uuid, location=claimed_location(self._settings.resolved_control_root, claim))

    @staticmethod
    def _remember[V](memory: dict[str, V], key: str, value: V) -> None:
        memory.pop(key, None)
        memory[key] = value
        while len(memory) > _REMEMBERED:
            memory.pop(next(iter(memory)))
