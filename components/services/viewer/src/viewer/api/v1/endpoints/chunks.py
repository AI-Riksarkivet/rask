from fastapi import APIRouter

from viewer.api.dependencies import RayClientDep, SessionDep, SettingsDep
from viewer.core.exceptions import ServiceUnavailableError
from viewer.schemas.chunk import ChunkListResponse, SubmitChunkResponse
from viewer.services import chunks as chunks_service
from viewer.services import submission as submission_service


router = APIRouter(prefix="/chunks", tags=["chunks"])


@router.get("/")
async def list_chunks(session: SessionDep) -> ChunkListResponse:
    return await chunks_service.list_chunks(session)


@router.post("/{chunk_id}/submit")
async def submit_chunk(
    chunk_id: int,
    session: SessionDep,
    settings: SettingsDep,
    ray_client: RayClientDep,
) -> SubmitChunkResponse:
    if ray_client is None:
        raise ServiceUnavailableError("Ray dashboard unreachable")
    return await submission_service.submit_chunk(
        session,
        ray_client,
        chunk_id=chunk_id,
        repo_root=settings.repo_root,
    )
