"""The door maintenance finishes what a failed door left to announce through, at ``/management/v1/announcement-intents``.

A door that changes state files what it will announce before the change and removes the filing once announced
(:mod:`catalog.api.announcing`, [[LH-576]]). A filing its door left, by failing or by dying, is finished here when
maintenance's hourly audit asks: maintenance holds no emitter of the catalog's and signs for none of the catalog's
callers, so the sending is the catalog's, judged on its own reads. Asked on ``can_reclaim_trash`` on the estate root,
the rung the trash, clone-ending and idempotency-record doors ask, which only the ``reclaimer`` role opens: no new
grant. Each finish is recorded on the audit trail, since it sends on the catalog's own signature.

The router guard leaves this path to its handler (``fga_deps._RESOURCES`` names none of it), so the gate is this
router's own dependency, as the trash doors' is.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from catalog.api.announcing import AnnouncerDep
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints.trash_reclaim import require_can_reclaim_trash
from catalog.schemas import AnnouncementIntentsFinishResponse
from catalog.services import announcement_intents
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit


router = APIRouter()
management_router = APIRouter(prefix="/management/v1/announcement-intents", tags=["announcement-intents"], dependencies=[Depends(require_can_reclaim_trash)])

#: The most filings one call judges, so a call stays bounded however long the audit was away; the audit asks again while
#: any are left.
FINISH_LIMIT = 200


@management_router.post("/finish")
async def finish_announcement_intents(token: CurrentToken, announcer: AnnouncerDep) -> AnnouncementIntentsFinishResponse:
    """Send what each filing a failed door left names, wherever the store shows its change landed, and remove the filing.

    A filing younger than a door can act is left to its door. Each announcement goes out as its door built it, under its
    own id, so one the door had sent before failing is the same event again.
    \f

    Args:
        token: The verified caller.
        announcer: Where the filings are judged and sent from.

    Returns:
        How many filings it removed, announcements it sent and left, and filings it left.
    """
    done = await announcer.finish(limit=FINISH_LIMIT)
    audit(
        AuditAction.ANNOUNCEMENT_INTENTS_FINISH,
        AuditOutcome.SUCCESS,
        subject=token.subject if token is not None else None,
        resource=f"control:{announcement_intents.PREFIX}",
        finished=done.finished,
        sent=done.sent,
        unlanded=done.unlanded,
    )
    return done
