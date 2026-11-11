"""The doors maintenance finishes a registered clone's ending through (ADR 0198 §1), at ``/management/v1/clone-endings``.

A door that ends a clone files its ending before its row goes and removes it once the clone's tags on its sources and
its location's claim are released (:mod:`catalog.services.clone_endings`). One it could not remove is listed here, and
its finish is asked for by maintenance's hourly audit: maintenance's store policy writes no source's ``_refs/tags``, so
the release is the catalog's, judged on its own reads. Both doors ask ``can_reclaim_trash`` on the estate root, the
rung the trash doors ask (``trash_reclaim``), which only the ``reclaimer`` role opens, seeded for maintenance's
ServiceAccount by the chart's bootstrap-admin hook: no new grant. Each finish is recorded on the audit trail, since it
deletes tags and releases a claim on the catalog's own session.

The router guard leaves these paths to their handlers (``fga_deps._RESOURCES`` names none of them), so the gate is this
router's own dependency, as the trash doors' is.
"""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Path
from fastapi.concurrency import run_in_threadpool
from lance_namespace import LanceNamespaceError

from catalog.api.dependencies import NamespaceDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints.trash_reclaim import require_can_reclaim_trash
from catalog.schemas import CloneEndingEntry, CloneEndingFinishResponse, CloneEndingsResponse
from catalog.services import clone_endings
from lakehouse.governed import fga
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit


router = APIRouter()
management_router = APIRouter(prefix="/management/v1/clone-endings", tags=["trash-reclaim"], dependencies=[Depends(require_can_reclaim_trash)])

#: A digest names one location: the SHA-256 its tags, its claim and its endings are kept under.
DigestPath = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]
#: The filing request's token, which names one ending under the digest.
FilingPath = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


@management_router.get("")
async def list_clone_endings(settings: SettingsDep) -> CloneEndingsResponse:
    """Every registered clone's ending its door did not remove, oldest first: each a finish to ask for."""
    endings = await run_in_threadpool(clone_endings.listing, settings)
    return CloneEndingsResponse(
        endings=[CloneEndingEntry(digest=ending.digest, filing=ending.filing, door=ending.door, filed_at=ending.filed_at) for ending in endings]
    )


@management_router.post("/{digest}/{filing}/finish")
async def finish_clone_ending(
    digest: DigestPath, filing: FilingPath, ns: NamespaceDep, settings: SettingsDep, token: CurrentToken
) -> CloneEndingFinishResponse:
    """Release what the door that ended a registered clone named (its tag on each source, its base record where its bytes
    went, its location's claim, fenced while the finish acts), and remove the ending last.

    Refused 409 (code 19) while the ending is younger than a location claim's lease, while another finish acts on it,
    while another table holds the location, and while the clone itself stands there, until a day after the filing, when
    the ending is removed with nothing released; 409 (code 14) when the ending or the claim changed under the finish. No
    such ending answers 404.
    """
    subject = token.subject if token is not None else None
    try:
        outcome = await run_in_threadpool(clone_endings.finish, ns, settings, digest, filing, token=uuid4().hex)
    except LanceNamespaceError as refused:
        audit(AuditAction.CLONE_ENDING_FINISH, AuditOutcome.FAILURE, subject=subject, resource=f"clone_ending:{digest}/{filing}", reason=str(refused)[:300])
        raise
    # Named as the trash doors name theirs: the clone's object, and each source whose tag the finish deleted.
    audit(
        AuditAction.CLONE_ENDING_FINISH,
        AuditOutcome.SUCCESS,
        subject=subject,
        resource=fga.table_object(outcome.uuid) if outcome.uuid is not None else f"table:{outcome.table}",
        table=outcome.table,
        ending=f"{digest}/{filing}",
        sources_released=",".join(outcome.released),
        kept=outcome.kept,
    )
    return CloneEndingFinishResponse(tags_released=len(outcome.released), kept=outcome.kept)
