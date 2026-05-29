from typing import Annotated

from fastapi import APIRouter, Body

from viewer.api.dependencies import RayClientDep, SessionDep, SettingsDep
from viewer.core.exceptions import ServiceUnavailableError
from viewer.models.pipelines import PIPELINE_SPECS
from viewer.repositories import batch as batch_repo
from viewer.schemas.chunk import ChunkListResponse, StopResult, SubmitRequest, SubmitResult
from viewer.services import submission as submission_service


router = APIRouter(prefix="/chunks", tags=["chunks"])

# Default body when the POST carries none — a module-level singleton (FastAPI
# never mutates it; a real request builds a fresh SubmitRequest from the body).
_DEFAULT_SUBMIT = SubmitRequest()


@router.get("/")
async def list_chunks(session: SessionDep) -> ChunkListResponse:
    return ChunkListResponse(chunks=await batch_repo.chunks_summary(session))


@router.post("/{chunk_id}/submit")
async def submit_chunk(
    chunk_id: int,
    session: SessionDep,
    settings: SettingsDep,
    ray_client: RayClientDep,
    body: Annotated[SubmitRequest, Body()] = _DEFAULT_SUBMIT,
) -> SubmitResult:
    # No concurrency cap here: Ray/Kueue queue what doesn't fit, and the
    # orchestrator's per-chunk in-flight guard prevents the auto-loop from
    # re-submitting a running chunk. So different pipelines (or chunks) can run
    # concurrently up to the cluster's resources.
    if ray_client is None:
        raise ServiceUnavailableError("Ray dashboard unreachable")
    return await submission_service.submit_chunk(
        session,
        ray_client,
        chunk_id=chunk_id,
        params=settings.runner_params(),
        spec=PIPELINE_SPECS[body.pipeline],
    )


@router.post("/{chunk_id}/stop")
async def stop_chunk(
    chunk_id: int,
    session: SessionDep,
    ray_client: RayClientDep,
) -> StopResult:
    if ray_client is None:
        raise ServiceUnavailableError("Ray dashboard unreachable")
    return await submission_service.stop_chunk(session, ray_client, chunk_id=chunk_id)
