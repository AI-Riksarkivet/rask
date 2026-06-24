import mimetypes
import re
from datetime import timedelta
from pathlib import PurePosixPath

from anyio import to_thread
from fastapi import APIRouter, File, UploadFile, status

from core.api.dependencies import CatalogTblDep, S3Dep, SessionDep, SettingsDep
from core.models.batch import BatchPublic
from core.models.enums import HtrStatus
from core.schemas.batch import BatchListResponse, RandomBatchResponse
from core.schemas.catalog import CatalogHit
from core.schemas.sync import SyncResponse
from core.services import batches as batches_service
from core.services import registration
from core.services.discover import catalog as catalog_service
from core.services.sync import reconcile_from_s3
from service_kit.exceptions import NotFoundError, UnprocessableEntityError


_VOLUME_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff")

router = APIRouter(prefix="/batches", tags=["batches"])


@router.get("/")
async def list_batches(session: SessionDep) -> BatchListResponse:
    return await batches_service.list_batches(session)


@router.get("/random")
async def random_batch(session: SessionDep, status: HtrStatus = HtrStatus.DONE) -> RandomBatchResponse:
    batch_id = await batches_service.random_batch(session, status)
    return RandomBatchResponse(batch_id=batch_id, status=status)


@router.post("/{batch_id}/register", status_code=status.HTTP_201_CREATED)
async def register_batch(batch_id: str, session: SessionDep, settings: SettingsDep, s3: S3Dep) -> BatchPublic:
    """Index an already-uploaded input-bucket prefix `{batch_id}/` as a one-chunk volume."""
    batch = await registration.register_volume(session, s3, input_bucket=settings.cache_bucket, volume_id=batch_id)
    return BatchPublic.model_validate(batch)


@router.post("/{batch_id}/upload", status_code=status.HTTP_201_CREATED)
async def upload_batch(
    batch_id: str,
    session: SessionDep,
    settings: SettingsDep,
    s3: S3Dep,
    files: list[UploadFile] = File(...),  # noqa: B008 — FastAPI dependency injection pattern
) -> BatchPublic:
    """Upload image files to `images-batch/<batch_id>/`, then register the volume.

    Browser -> gateway -> here -> internal S3 put -> register_volume. Non-image
    files are skipped; filenames are reduced to their basename (no path traversal).
    """
    if not _VOLUME_ID_RE.match(batch_id):
        raise UnprocessableEntityError(f"invalid volume id {batch_id!r}: use letters, digits, '-', '_' only")

    written = 0
    for upload in files:
        name = PurePosixPath(upload.filename or "").name  # basename only — blocks ../ traversal
        if not name or not name.lower().endswith(_IMAGE_SUFFIXES):
            continue
        data = await upload.read()
        key = f"{batch_id}/{name}"
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        await to_thread.run_sync(
            lambda k=key, d=data, ct=content_type: s3.put_object(
                Bucket=settings.cache_bucket, Key=k, Body=d, ContentType=ct
            )
        )
        written += 1

    if written == 0:
        raise UnprocessableEntityError("no image files in upload (allowed: jpg, jpeg, png, tif, tiff)")

    batch = await registration.register_volume(session, s3, input_bucket=settings.cache_bucket, volume_id=batch_id)
    return BatchPublic.model_validate(batch)


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
async def sync_batches(session: SessionDep, settings: SettingsDep, s3: S3Dep) -> SyncResponse:
    # S3Dep yields a 503 when S3 is unconfigured (no S3 endpoint set), so this
    # needs no explicit endpoint guard.
    await reconcile_from_s3(session, s3, cache_bucket=settings.cache_bucket, output_bucket=settings.output_bucket)
    payload = await batches_service.list_batches(session)
    return SyncResponse(**payload.model_dump())
