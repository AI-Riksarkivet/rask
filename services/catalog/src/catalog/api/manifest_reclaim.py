"""The tick that reclaims each root's ``__manifest`` history ([[LH-251]]).

Every namespace operation writes the namespace index again as a new version and the ``dir`` backend reclaims none of
them (`catalog.services.manifest_reclaim`), so this tick removes the versions older than ``manifest_retain_seconds`` on
every root: level-triggered, idempotent and late-is-free. It runs on the catalog's own timer (`catalog.api.timers`),
once a window whatever the replica count.

A reclaim that overlaps another is harmless all the same: two reclaims of one root remove the same versions, and the
store answers a delete of an object already gone as done. A root that fails is logged and counted on
``catalog.manifest_reclaim.failed``, which pages once it keeps failing (``CatalogManifestReclaimFailing``), and leaves
every other root to this tick.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from functools import partial

from fastapi.concurrency import run_in_threadpool
from opentelemetry import metrics
from pydantic import BaseModel, Field

from catalog.api.ticks import Tick
from catalog.services import manifest_reclaim
from catalog.services.manifest_reclaim import RootReclaim


log = logging.getLogger(__name__)

_meter = metrics.get_meter("lance.catalog")
_failed = _meter.create_counter(
    "catalog.manifest_reclaim.failed",
    unit="{root}",
    description="Roots whose __manifest history a reclaim tick could not reclaim ([[LH-251]]): while one keeps failing,"
    " every namespace operation on it keeps a version again, and a dropped id stays readable in that history. Which root"
    " is the `manifest_reclaim_failed` log line's to say: a root's URI names a tenant's bucket, and a series per root"
    " grows with the estate.",
)


def create_series() -> None:
    """Create the failure counter's series at 0 when the catalog starts its timers, so the first failed root is an
    increase an alert can read: a series born at 1 has no 0 before it."""
    _failed.add(0)


class ManifestReclaimTick(BaseModel):
    """What one tick did: each root's reclaim, and how many roots failed."""

    reclaimed: list[RootReclaim] = Field(default_factory=list)
    failed: int = 0


async def tick(ctx: Tick) -> ManifestReclaimTick:
    """Remove each root's index versions older than the retention, one root at a time."""
    settings = ctx.settings
    older_than = timedelta(seconds=settings.manifest_retain_seconds)
    done_this_tick = ManifestReclaimTick()
    for root in await run_in_threadpool(manifest_reclaim.roots, settings):
        # One root at a time, so a fault on one leaves every other root to this tick and the next.
        try:
            done = await run_in_threadpool(partial(manifest_reclaim.reclaim, root, settings.storage_options(), older_than=older_than))
        # pylance raises each Lance error as one of these three (`python/src/error.rs` at v13.0.0).
        except (OSError, ValueError, RuntimeError):
            log.warning("manifest_reclaim_failed", extra={"root": root}, exc_info=True)
            _failed.add(1)
            done_this_tick.failed += 1
            continue
        if done is not None:
            log.info("manifest_reclaimed", extra=done.model_dump())
            done_this_tick.reclaimed.append(done)
    return done_this_tick
