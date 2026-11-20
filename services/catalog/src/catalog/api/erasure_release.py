"""The tick that releases an erasure's pinning tags and branches when their grace period ends ([[LH-178]]).

ADR 0138 requires the pending release to survive a restart, so it is a record with a due time on the control
root (`catalog.services.erasure_release`), and this tick is the convergence that acts on it: level-triggered,
idempotent, late-is-free. It runs on the catalog's own timer (`catalog.api.timers`), once a window whatever the
replica count, and each release moves by an ETag-guarded write besides.

The catalog hosts it because the release is ref-plane work on a table the catalog serves, and the events it
emits are the catalog's own ref-plane verbs (`table_tag_deleted`, `table_branch_deleted`,
`table_branch_created`), untargeted as their doors emit them: the person was told by the scheduled notice.
"""

from __future__ import annotations

import logging
from functools import partial

import lance
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from catalog.api.ticks import Tick
from catalog.services import erasure_release, table_claims
from lakehouse.events.control_emit import emit_control
from lakehouse.governed import fga
from lakehouse.lance_session import fresh_session, open_dataset
from lakehouse.location_claims import ClaimStore
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)


class ErasureReleaseTick(BaseModel):
    """What one tick did."""

    changed: int = 0


def _open(location: str, storage_options: StorageOptions) -> lance.LanceDataset:
    return open_dataset(location, storage_options=storage_options.in_clear(), session=fresh_session())


async def tick(ctx: Tick) -> ErasureReleaseTick:
    """Release every due release, then announce each ref it changed."""
    settings = ctx.settings
    changes = await run_in_threadpool(
        erasure_release.release_due,
        settings.registry_root,
        settings.storage_options(),
        now=ctx.clock(),
        open_location=partial(_open, storage_options=ctx.storage_options),
    )
    claims = table_claims.claim_store(settings)
    for change in changes:
        table = await run_in_threadpool(_named_now, claims, change)
        await emit_control(
            ctx.control,
            action=change.action,
            object_type="table",
            object_id=fga.table_object(change.table_uuid),
            object_name=table,
            actor=None,
            extra=change.extra,
        )
    log.info("erasure_release_tick", extra={"changed": len(changes)})
    return ErasureReleaseTick(changed=len(changes))


def _named_now(claims: ClaimStore, change: erasure_release.RefChange) -> str:
    """The changed table as it is named now, which a rename during the grace period changed, else the name the release
    kept: the refs have changed already, so a claim that cannot be read costs the event its new name, never the event."""
    try:
        return table_claims.current_name(claims, change.location, change.table_uuid) or change.table
    except (OSError, ValueError) as exc:
        log.warning("erasure_release_table_unread", extra={"table": change.table, "error": str(exc)[:300]})
        return change.table
