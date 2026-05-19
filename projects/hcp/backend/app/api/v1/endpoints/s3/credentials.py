"""S3 presigned URL generation and credential derivation endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import (
    get_s3_service,
    get_s3_settings,
    get_storage_settings,
)
from app.api.errors import run_storage
from app.core.auth_utils import derive_s3_keys
from app.core.config import S3Settings, StorageSettings
from app.core.security import (
    HcpCredentials,
    oauth2_scheme,
    verify_token_with_credentials,
)
from app.core.tenant_routing import s3_endpoint_for_tenant
from app.schemas.s3 import (
    PresignedUrlRequest,
    PresignedUrlResponse,
    S3CredentialsResponse,
)
from app.services.storage import StorageProtocol

router = APIRouter(tags=["S3 Credentials"])

_ALLOWED_METHODS = {"get_object", "put_object"}


@router.post("/presign", response_model=PresignedUrlResponse)
async def generate_presigned_url(
    body: PresignedUrlRequest,
    s3: StorageProtocol = Depends(get_s3_service),
):
    """Generate a presigned URL for temporary access to an object.

    Use `get_object` to create a download link, or `put_object` for an upload link.
    The URL is valid for the specified duration (default 1 hour, max 7 days).
    Anyone with the URL can access the object — no credentials needed.
    """
    if body.method not in _ALLOWED_METHODS:
        raise HTTPException(
            status_code=422,
            detail=f"method must be one of: {', '.join(sorted(_ALLOWED_METHODS))}",
        )
    url = await run_storage(
        s3.generate_presigned_url(body.bucket, body.key, body.expires_in, body.method),
        f"object '{body.key}'",
    )
    return PresignedUrlResponse(
        url=url,
        bucket=body.bucket,
        key=body.key,
        expires_in=body.expires_in,
        method=body.method,
    )


@router.get("/credentials", response_model=S3CredentialsResponse)
async def get_s3_credentials(
    token: Annotated[str, Depends(oauth2_scheme)],
    storage_settings: StorageSettings = Depends(get_storage_settings),
    s3_settings: S3Settings = Depends(get_s3_settings),
):
    """Return the S3 credentials for the authenticated user.

    HCP: derives keys from username/password (base64 + md5).
    MinIO/generic: returns the configured S3 access key and secret key.
    """
    if storage_settings.storage_backend == "hcp":
        creds: HcpCredentials = verify_token_with_credentials(token)
        access_key, secret_key = derive_s3_keys(creds.username, creds.password)
        endpoint_url = s3_endpoint_for_tenant(creds.tenant, s3_settings.hcp_domain)
        return S3CredentialsResponse(
            access_key_id=access_key,
            secret_access_key=secret_key,
            username=creds.username,
            tenant=creds.tenant,
            endpoint_url=endpoint_url,
        )

    return S3CredentialsResponse(
        access_key_id=storage_settings.s3_access_key,
        secret_access_key=storage_settings.s3_secret_key.get_secret_value(),
        endpoint_url=storage_settings.s3_endpoint_url or None,
    )
