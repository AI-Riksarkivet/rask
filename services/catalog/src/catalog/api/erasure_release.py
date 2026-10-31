"""The cron tick that releases an erasure's pinning tags and branches when their grace period ends ([[LH-178]]).

ADR 0138 requires the pending release to survive a restart, so it is a record with a due time on the control
root (`catalog.services.erasure_release`), and this door is the convergence that acts on it: level-triggered,
idempotent, late-is-free. That is the shape `.claude/skills/rask-dapr` rules a cron binding for, and the reason
the Jobs API is not used, exactly as for the control relay (`catalog/api/control_relay.py`).

**Delivered at the POD ROOT**, as ``POST /<component name>``, so the Component's name,
``LANCE_ERASURE_RELEASE_BINDING_NAME`` and the served path are one string. Guarded by ``require_dapr_token``:
an open door here would let anything that reaches the port run a due release early, which is harmless, or
probe which releases exist, which is not.

The catalog hosts it because the release is ref-plane work on a table the catalog serves, and the events it
emits are the catalog's own ref-plane verbs (`table_tag_deleted`, `table_branch_deleted`,
`table_branch_created`), untargeted as their doors emit them: the person was told by the scheduled notice.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Annotated

import lance
from fastapi import Depends, FastAPI
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from catalog.api.cron import mount_cron
from catalog.api.dependencies import ClockDep, ControlEmitterDep, SettingsDep, StorageOptionsDep
from catalog.core.config import fresh_lance_session
from catalog.services import erasure_release, table_claims
from service_kit.control_emit import emit_control
from service_kit.governed import fga
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.lakehouse.location_claims import ClaimStore
from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)

#: One tick at a time per process: a second tick arriving while one runs would only lose every claim to it.
_tick_lock = asyncio.Lock()


class ErasureReleaseTick(BaseModel):
    """What one tick did."""

    skipped: bool = False
    changed: int = 0


def _open(location: str, storage_options: StorageOptions) -> lance.LanceDataset:
    return lance.dataset(location, storage_options=storage_options.in_clear(), session=fresh_lance_session())


async def _on_cron(
    settings: SettingsDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    clock: ClockDep,
    _: Annotated[None, Depends(require_dapr_token)],
) -> ErasureReleaseTick:
    """Release every due release, then announce each ref it changed."""
    if _tick_lock.locked():
        return ErasureReleaseTick(skipped=True)
    async with _tick_lock:
        changes = await run_in_threadpool(
            erasure_release.release_due, settings.registry_root, settings.storage_options(), now=clock(), open_location=partial(_open, storage_options=so)
        )
    claims = table_claims.claim_store(settings)
    for change in changes:
        table = await run_in_threadpool(_named_now, claims, change)
        await emit_control(
            control, action=change.action, object_type="table", object_id=fga.table_object(change.table_uuid), object_name=table, actor=None, extra=change.extra
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


def mount_erasure_release(app: FastAPI, binding_name: str) -> bool:
    """Mount the tick at the exact binding name when one is configured; report whether it was."""
    return mount_cron(app, binding_name, _on_cron, tag="erasure-release")
