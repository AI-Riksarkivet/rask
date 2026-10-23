"""The cron tick that expires a promotion hold nobody answered before its review deadline (ADR 0156 C1, [[LH-466]]).

A hold is a record with a deadline on the control root (`catalog.services.promotions`), set from the project's
declared `review_hours`, and this door is the convergence that acts on it: level-triggered, idempotent,
late-is-free, the shape `.claude/skills/rask-dapr` rules a cron binding for. Expiry decides nothing about the data:
`published` stays where it was and main keeps the held version, so readers see what they saw. The held version's
pin is removed, and the expiry is announced as `promotion_expired`, untargeted like the other hold events.

**Delivered at the POD ROOT**, as ``POST /<component name>``, so the Component's name,
``LANCE_PROMOTION_EXPIRY_BINDING_NAME`` and the served path are one string. Guarded by ``require_dapr_token``.

**EVERY REPLICA TICKS, AND EACH HOLD EXPIRES ONCE.** A cron binding fires on every replica; a tick moves a hold
out of ``open`` only through an ETag-guarded write that finds it still open past its deadline, so the replica that
loses the write announces nothing. Holds are expired one at a time, so a store fault on one leaves the others due.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import NamespaceNotFoundError, TableNotFoundError
from pydantic import BaseModel

from catalog.api.dependencies import ClockDep, ControlEmitterDep, SettingsDep, StorageOptionsDep, namespace_for_top_ns
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier
from catalog.services import promotions, table_claims
from service_kit.control_emit import emit_control
from service_kit.governed import fga
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.lakehouse.records import RecordChangedError


log = logging.getLogger(__name__)

#: One tick at a time per process: a second tick arriving while one runs would only lose every write to it.
_tick_lock = asyncio.Lock()


class PromotionExpiryTick(BaseModel):
    """What one tick did."""

    skipped: bool = False
    expired: int = 0


async def _on_cron(
    request: Request,
    settings: SettingsDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    clock: ClockDep,
    _: Annotated[None, Depends(require_dapr_token)],
) -> PromotionExpiryTick:
    """Expire every open hold past its deadline, one at a time: announce it and remove its pin."""
    if _tick_lock.locked():
        return PromotionExpiryTick(skipped=True)
    expired = 0
    async with _tick_lock:
        now = clock()
        for hold in await run_in_threadpool(partial(promotions.due, settings.registry_root, settings.storage_options(), now=now)):
            # One hold at a time, so a fault on one leaves every other due hold to this tick and the next.
            try:
                closed = await run_in_threadpool(partial(promotions.expire, settings.registry_root, settings.storage_options(), hold, now=now))
            except (OSError, RecordChangedError):
                log.warning("promotion_expiry_failed", extra={"table": hold.table}, exc_info=True)
                continue
            if closed is None:
                continue
            expired += 1
            table = await _named_now(settings, closed)
            await emit_control(
                control,
                action="promotion_expired",
                object_type="table",
                object_id=fga.table_object(closed.uuid),
                object_name=table or closed.table,
                actor=None,
                extra={"version": closed.version, "published_version": closed.published_version},
            )
            if table is not None:
                await _unpin(request, settings, so, table, closed.uuid)
    log.info("promotion_expiry_tick", extra={"expired": expired})
    return PromotionExpiryTick(expired=expired)


async def _named_now(settings: Settings, hold: promotions.Hold) -> str | None:
    """The expired hold's table as it is named now, which a rename since the hold changed; ``None`` when it is gone.

    A claim that cannot be read is logged and read as gone: the expiry is already recorded, and its announcement must
    not be lost to it. The pin then stays until the table is held or decided again.
    """
    try:
        table = await run_in_threadpool(table_claims.current_name, table_claims.claim_store(settings), hold.location, hold.uuid)
    except (OSError, ValueError):
        log.warning("promotion_expired_table_unread", extra={"table": hold.table}, exc_info=True)
        return None
    if table is None:
        log.info("promotion_expired_table_gone", extra={"table": hold.table})
    return table


async def _unpin(request: Request, settings: Settings, so: dict[str, str], table: str, table_uuid: str) -> None:
    """Sync the hold pin of an expired hold's table, named ``table`` now, to its record, which an expiry leaves without one.

    A pin that cannot be removed now stays on its version until the table is held or decided again, which syncs the
    pin to the record; a table that is gone took its pins with it.
    """
    segments = parse_identifier(table)
    try:
        ns = await namespace_for_top_ns(request, settings, segments[0])
        current = await run_in_threadpool(promotions.hold_of, settings.registry_root, settings.storage_options(), table_uuid)
        await run_in_threadpool(promotions.sync_pin, ns, so, segments, current)
    except (NamespaceNotFoundError, TableNotFoundError):
        log.info("promotion_expired_table_gone", extra={"table": table})
    except (OSError, RecordChangedError):
        log.warning("promotion_unpin_failed", extra={"table": table}, exc_info=True)


async def _ack_binding() -> dict[str, str]:
    """Dapr's startup pre-flight (``OPTIONS /<binding name>``): a 2xx confirms this app consumes the binding."""
    return {"status": "ok"}


def mount_promotion_expiry(app: FastAPI, binding_name: str) -> bool:
    """Mount the tick at the exact binding name when one is configured; report whether it was."""
    if not binding_name:
        return False
    router = APIRouter()
    router.add_api_route(f"/{binding_name}", _on_cron, methods=["POST"], tags=["promotion-expiry"])
    router.add_api_route(f"/{binding_name}", _ack_binding, methods=["OPTIONS"], include_in_schema=False)
    app.include_router(router)
    return True
