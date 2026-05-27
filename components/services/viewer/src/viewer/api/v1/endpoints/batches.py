from fastapi import APIRouter

from viewer.api.dependencies import SessionDep, SettingsDep
from viewer.models.batch import BatchPublic
from viewer.models.enums import HtrStatus
from viewer.schemas.batch import BatchListResponse, RandomBatchResponse, SyncResponse
from viewer.services import batches as batches_service
from viewer.services import submission


router = APIRouter(tags=["batches"])


@router.get("/api/batches")
async def list_batches(session: SessionDep) -> BatchListResponse:
    return await batches_service.list_batches(session)


@router.get("/api/batches/random")
async def random_batch(session: SessionDep, status: HtrStatus = HtrStatus.DONE) -> RandomBatchResponse:
    batch_id = await batches_service.random_batch(session, status)
    return RandomBatchResponse(batch_id=batch_id, status=status)


@router.get("/api/batches/{batch_id}")
async def get_batch(batch_id: str, session: SessionDep) -> BatchPublic:
    return await batches_service.get_batch(session, batch_id)


@router.post("/api/batches/sync")
async def sync_batches(settings: SettingsDep, session: SessionDep) -> SyncResponse:
    await submission.run_sync_script(
        scripts_dir=settings.resolved_scripts_dir,
        repo_root=settings.repo_root,
        timeout=settings.sync_timeout_seconds,
    )
    payload = await batches_service.list_batches(session)
    return SyncResponse(**payload.model_dump())
