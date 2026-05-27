from fastapi import APIRouter

from viewer.api.dependencies import SessionDep, SettingsDep
from viewer.schemas.chunk import ChunkListResponse, SubmitChunkResponse
from viewer.services import chunks as chunks_service
from viewer.services import submission


router = APIRouter(tags=["chunks"])


@router.get("/api/chunks")
async def list_chunks(session: SessionDep) -> ChunkListResponse:
    return await chunks_service.list_chunks(session)


@router.post("/api/chunks/{chunk_id}/submit")
async def submit_chunk(chunk_id: int, settings: SettingsDep) -> SubmitChunkResponse:
    stdout = await submission.submit_chunk(
        scripts_dir=settings.resolved_scripts_dir,
        repo_root=settings.repo_root,
        chunk_id=chunk_id,
        timeout=settings.submit_timeout_seconds,
    )
    return SubmitChunkResponse(chunk_id=chunk_id, stdout=stdout)
