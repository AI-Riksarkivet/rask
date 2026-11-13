"""The door maintenance reclaims expired `Idempotency-Key` records through, at ``/management/v1/idempotency-keys``.

A keyed write leaves a record under ``_idempotency/`` on the control root, which answers a retry of the write for the
catalog's lifetime (``LANCE_IDEMPOTENCY_LIFETIME_SECONDS``) and is read by no request after it. Maintenance's daily
scan asks for those records to go ([[LH-576]]): maintenance's store policy writes nothing of the control root, so the
delete is the catalog's, judged on its own reads (:func:`lakehouse.idempotency.reclaim`). Asked on
``can_reclaim_trash`` on the estate root, the rung the trash and clone-ending doors ask, which only the ``reclaimer``
role opens, granted to maintenance's ServiceAccount by the catalog's declaration: no new grant. Each reclaim is
recorded on the audit trail, since it deletes records on the catalog's own session.

The router guard leaves this path to its handler (``fga_deps._RESOURCES`` names none of it), so the gate is this
router's own dependency, as the trash doors' is.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from catalog.api.dependencies import SettingsDep
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints.trash_reclaim import require_can_reclaim_trash
from catalog.schemas import IdempotencyKeysReclaimResponse
from lakehouse import idempotency
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit


router = APIRouter()
management_router = APIRouter(prefix="/management/v1/idempotency-keys", tags=["idempotency-keys"], dependencies=[Depends(require_can_reclaim_trash)])

#: The most records one call reclaims, so a call stays bounded however long the scan was away; the scan asks again while
#: any are left.
RECLAIM_LIMIT = 1_000


@management_router.post("/reclaim")
async def reclaim_idempotency_keys(settings: SettingsDep, token: CurrentToken) -> IdempotencyKeysReclaimResponse:
    """Delete the `Idempotency-Key` records nothing has written for their lifetime, at most a thousand, oldest first.

    A record a retry wrote again after the listing is kept for another lifetime. ``left`` counts the expired records
    past this call's limit, for the caller to ask again.
    \f

    Args:
        settings: The catalog's settings: the control root, and the records' lifetime.
        token: The verified caller.

    Returns:
        How many records went, were kept, and are left.
    """
    lifetime = timedelta(seconds=settings.idempotency_lifetime_seconds)
    done = await run_in_threadpool(idempotency.reclaim, settings.registry_root, settings.storage_options(), lifetime=lifetime, limit=RECLAIM_LIMIT)
    audit(
        AuditAction.IDEMPOTENCY_KEYS_RECLAIM,
        AuditOutcome.SUCCESS,
        subject=token.subject if token is not None else None,
        resource=f"control:{idempotency.PREFIX}",
        reclaimed=done.reclaimed,
        kept=done.kept,
        left=done.left,
    )
    return IdempotencyKeysReclaimResponse.model_validate(done.model_dump())
