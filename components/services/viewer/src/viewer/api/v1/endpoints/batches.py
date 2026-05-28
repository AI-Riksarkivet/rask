from datetime import timedelta

from fastapi import APIRouter

from viewer.api.dependencies import CatalogTblDep, SessionDep, SettingsDep
from viewer.core.exceptions import NotFoundError, ServiceUnavailableError
from viewer.models.batch import BatchPublic
from viewer.models.enums import HtrStatus
from viewer.schemas.batch import BatchListResponse, RandomBatchResponse
from viewer.schemas.sync import SyncResponse
from viewer.schemas.catalog import CatalogHit
from viewer.services import batches as batches_service
from viewer.services import catalog as catalog_service
from viewer.services.sync import reconcile_from_s3


router = APIRouter(prefix="/batches", tags=["batches"])


@router.get("/")
async def list_batches(session: SessionDep) -> BatchListResponse:
    return await batches_service.list_batches(session)


@router.get("/random")
async def random_batch(session: SessionDep, status: HtrStatus = HtrStatus.DONE) -> RandomBatchResponse:
    batch_id = await batches_service.random_batch(session, status)
    return RandomBatchResponse(batch_id=batch_id, status=status)


@router.get("/{batch_id}")
async def get_batch(batch_id: str, session: SessionDep) -> BatchPublic:
    return await batches_service.get_batch(session, batch_id)


@router.get("/{batch_id}/catalog")
async def get_batch_catalog(batch_id: str, tbl: CatalogTblDep, settings: SettingsDep) -> CatalogHit:
    """EAD catalog row for a batch — joined by `bild_id == batch_id`."""
    hit = await catalog_service.by_bild_id(tbl, batch_id, timedelta(seconds=settings.lance_query_timeout_seconds))
    if hit is None:
        raise NotFoundError(f"no catalog entry for {batch_id}")
    return hit


@router.post("/sync")
async def sync_batches(session: SessionDep, settings: SettingsDep) -> SyncResponse:
    if not settings.hcp_endpoint:
        raise ServiceUnavailableError("HCP_ENDPOINT not configured")
    await reconcile_from_s3(
        session,
        hcp_endpoint=settings.hcp_endpoint,
        cache_bucket=settings.cache_bucket,
        output_bucket=settings.output_bucket,
    )
    payload = await batches_service.list_batches(session)
    return SyncResponse(**payload.model_dump())
