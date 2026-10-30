"""The cron tick that reclaims each root's ``__manifest`` history ([[LH-251]]).

Every namespace operation writes the namespace index again as a new version and the ``dir`` backend reclaims none of
them (`catalog.services.manifest_reclaim`), so this tick removes the versions older than ``manifest_retain_seconds`` on
every root: level-triggered, idempotent and late-is-free, the shape `.claude/skills/rask-dapr` rules a cron binding for.

**Delivered at the POD ROOT**, as ``POST /<component name>``, so the Component's name,
``LANCE_MANIFEST_RECLAIM_BINDING_NAME`` and the served path are one string. Guarded by ``require_dapr_token``.

**EVERY REPLICA TICKS, AND THAT IS HARMLESS.** Two reclaims of one root remove the same versions, and the store answers
a delete of an object already gone as done. A root that fails is logged and leaves every other root to this tick.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from functools import partial
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from catalog.api.cron import mount_cron
from catalog.api.dependencies import SettingsDep
from catalog.services import manifest_reclaim
from catalog.services.manifest_reclaim import RootReclaim
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)

#: One tick at a time per process: a second tick arriving while one runs would only repeat its deletes.
_tick_lock = asyncio.Lock()


class ManifestReclaimTick(BaseModel):
    """What one tick did: each root's reclaim, and how many roots failed."""

    skipped: bool = False
    reclaimed: list[RootReclaim] = Field(default_factory=list)
    failed: int = 0


async def _on_cron(settings: SettingsDep, _: Annotated[None, Depends(require_dapr_token)]) -> ManifestReclaimTick:
    """Remove each root's index versions older than the retention, one root at a time."""
    if _tick_lock.locked():
        return ManifestReclaimTick(skipped=True)
    older_than = timedelta(seconds=settings.manifest_retain_seconds)
    tick = ManifestReclaimTick()
    async with _tick_lock:
        for root in await run_in_threadpool(manifest_reclaim.roots, settings):
            # One root at a time, so a fault on one leaves every other root to this tick and the next.
            try:
                done = await run_in_threadpool(partial(manifest_reclaim.reclaim, root, settings.storage_options(), older_than=older_than))
            # pylance raises each Lance error as one of these three (`python/src/error.rs` at v13.0.0).
            except (OSError, ValueError, RuntimeError):
                log.warning("manifest_reclaim_failed", extra={"root": root}, exc_info=True)
                tick.failed += 1
                continue
            if done is not None:
                log.info("manifest_reclaimed", extra=done.model_dump())
                tick.reclaimed.append(done)
    return tick


def mount_manifest_reclaim(app: FastAPI, binding_name: str) -> bool:
    """Mount the tick at the exact binding name when one is configured; report whether it was."""
    return mount_cron(app, binding_name, _on_cron, tag="manifest-reclaim")
