"""Namespace custom metadata indexing routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service
from app.schemas.namespace import CustomMetadataIndexingSettings
from app.schemas.common import StatusResponse

router = APIRouter(tags=["Namespace: Indexing"])

PREFIX = "/tenants/{tenant_name}/namespaces"


@router.get(
    PREFIX + "/{ns_name}/customMetadataIndexingSettings",
    response_model=CustomMetadataIndexingSettings,
)
async def get_custom_metadata_indexing(
    tenant_name: str,
    ns_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/namespaces/{ns_name}/customMetadataIndexingSettings"
    )


@router.post(
    PREFIX + "/{ns_name}/customMetadataIndexingSettings", response_model=StatusResponse
)
async def modify_custom_metadata_indexing(
    tenant_name: str,
    ns_name: str,
    body: CustomMetadataIndexingSettings,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/namespaces/{ns_name}/customMetadataIndexingSettings",
        body=body,
    )
    return {"status": "updated"}
