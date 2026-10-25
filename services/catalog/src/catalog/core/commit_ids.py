"""The OpenLineage run id of a commit's announcement: derived from the commit, keyed by a secret only the catalog reads ([[LH-497]]).

A committed version is announced as a run whose id the catalog derives from the commit's own coordinates: the table's
uuid, the ref it landed on, that ref's incarnation and the version. The original announcement and a re-announcement the
reconciler makes from the version log (`catalog.services.commit_reconcile`) carry the same id, so lineage's feed, which
keys a run's terminal state on ``<runId>@COMPLETE``, lands one row however many arrive.

THE DERIVATION IS KEYED (owner, 2026-10-25). Lineage binds a run id to the author of its first recorded event and never
lets another extend it ([[LIN-5]]), and any producer holding ``can_write_data`` on some table may post a RunEvent with any
run id. A run id computable from public facts (a table's uuid is answered by DescribeTable and named on every feed row)
would let a writer of one table post another table's next commit ids first: that table's announcements would then be
refused as another author's run, and every re-announcement with them. HMAC-SHA256 under ``commit-id-key``, a secret the
catalog alone reads from the store, keeps the id deterministic for the catalog and unguessable to every other producer.

The key is never rotated: a commit announced under the old key and found again under a new one would be announced a
second time. A stack with no secret store derives under the empty key, as it emits unsigned: nothing there authenticates
a producer, so nothing is protected by a key it cannot hold.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import uuid
from contextlib import suppress
from typing import Final

from fastapi import FastAPI, Request

from service_kit.governed.secrets import fetch_dapr_secret
from service_kit.governed.signing_key import SigningKeyUnavailableError
from service_kit.probes import ReadyCheck
from service_kit.schemas.health import Readiness, ReadinessStatus


log = logging.getLogger(__name__)

#: The secret holding the key, and its field: hex text of at least :data:`MIN_KEY_BYTES` random bytes.
KEY_SECRET: Final = "commit-id-key"  # noqa: S105 — the secret's name, not its value
KEY_FIELD: Final = "key"
MIN_KEY_BYTES: Final = 32

#: The `app.state` attribute the lifespan publishes the holder under.
STATE: Final = "commit_ids"

#: Between resolutions once the key is held, and while it is not.
_REFRESH_SECONDS: Final = 300.0
_RETRY_SECONDS: Final = 15.0


def commit_run_id(key: bytes, *, table_uuid: str, branch: str | None, branch_identifier: str | None, version: int) -> str:
    """The run id of the announcement of ``version`` of ``table_uuid`` on ``branch`` (``None`` = main), in that branch's
    incarnation ``branch_identifier``: a UUIDv8 carrying 122 bits of HMAC-SHA256 under ``key``.

    The incarnation is part of it because a deleted and recreated branch restarts its numbering, so ``(branch, version)``
    alone names two commits (pylance 12 records a fresh ``branch_identifier`` per create).
    """
    message = "\x1f".join((table_uuid, branch or "", branch_identifier or "", str(version))).encode()
    value = int.from_bytes(hmac.new(key, message, hashlib.sha256).digest()[:16])
    # Version 8 and the RFC 9562 variant, set by hand: Python 3.13's `uuid.UUID(version=...)` takes 1 to 5 only.
    value = (value & ~(0xF << 76)) | (8 << 76)
    value = (value & ~(0x3 << 62)) | (0x2 << 62)
    return str(uuid.UUID(int=value))


class CommitIdKeyHolder:
    """The catalog's ``commit-id-key``, read through its own sidecar and re-read until held, then every five minutes."""

    def __init__(self, *, store: str) -> None:
        self._store = store
        self._key: bytes | None = None
        self._reason = "the commit id key has not been resolved yet"
        self._task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        return self._key is not None

    @property
    def reason(self) -> str:
        """Why the key is not held, for `/readyz`. Empty while held. Names no secret value."""
        return "" if self._key is not None else self._reason

    def key(self) -> bytes:
        """The key to derive under.

        Raises:
            SigningKeyUnavailableError: no key is held, so no announcement can carry its commit's id.
        """
        key = self._key
        if key is None:
            raise SigningKeyUnavailableError(f"the catalog cannot derive a commit's run id: {self._reason}")
        return key

    def resolve(self) -> bool:
        """Read the key once; answer whether one is held. Blocking: one sidecar read.

        A key once held is kept, through a store outage and through a changed value alike: it is never rotated, and
        an id derived under another key would announce every unconfirmed commit a second time.
        """
        text = fetch_dapr_secret(self._store, KEY_SECRET, retries=1).get(KEY_FIELD, "").strip()
        try:
            key = bytes.fromhex(text)
        except ValueError:
            key = b""
        if self._key is not None:
            if len(key) >= MIN_KEY_BYTES and not hmac.compare_digest(key, self._key):
                log.error("commit_id_key_changed", extra={"secret": KEY_SECRET})
            return True
        if len(key) < MIN_KEY_BYTES:
            reason = f"{KEY_SECRET} cannot be read from the secret store as {MIN_KEY_BYTES} or more hex bytes"
            if reason != self._reason:
                log.warning("commit_id_key_unresolved", extra={"reason": reason})
            self._reason = reason
            return False
        self._key, self._reason = key, ""
        log.info("commit_id_key_resolved")
        return True

    async def start(self) -> None:
        """Resolve once now, then keep re-resolving in the background until :meth:`stop`; an unresolved key fails no start."""
        await asyncio.to_thread(self.resolve)
        self._task = asyncio.create_task(self._refresh_forever(), name="commit-id-key")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _refresh_forever(self) -> None:
        while True:
            await asyncio.sleep(_REFRESH_SECONDS if self.ready else _RETRY_SECONDS)
            try:
                await asyncio.to_thread(self.resolve)
            except Exception:
                log.exception("commit_id_key_pass_failed")


class UnkeyedCommitIds:
    """The derivation of a stack with no secret store: the empty key, always held."""

    ready: Final = True
    reason: Final = ""

    def key(self) -> bytes:
        return b""


type CommitIds = CommitIdKeyHolder | UnkeyedCommitIds


def make_commit_ids(*, secrets_from_dapr: bool, store: str) -> CommitIds:
    """The key holder for a catalog reading its store, else the empty key."""
    return CommitIdKeyHolder(store=store) if secrets_from_dapr else UnkeyedCommitIds()


def commit_ids_of(app: FastAPI) -> CommitIds:
    """The holder the lifespan published on ``app``, or the empty key for an app no lifespan wired."""
    holder: CommitIds | None = getattr(app.state, STATE, None)
    return holder if holder is not None else UnkeyedCommitIds()


def commit_ids_ready_check(inner: ReadyCheck) -> ReadyCheck:
    """A `/readyz` check adding the commit id key to ``inner``: a catalog without it is `degraded` (503), as one
    without its signing key is, so it leaves its Service while `/livez` stays 200."""

    async def check(request: Request) -> Readiness:
        body = await inner(request)
        holder = commit_ids_of(request.app)
        if holder.ready:
            return body
        return body.model_copy(update={"status": ReadinessStatus.degraded, "components": {**body.components, "commit_ids": holder.reason}})

    return check
