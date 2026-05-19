"""Namespace compliance settings and retention class routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service
from app.schemas.namespace import ComplianceSettings
from app.schemas.common import StatusResponse
from app.schemas.retention_class import (
    RetentionClassCreate,
    RetentionClassList,
    RetentionClassResponse,
    RetentionClassUpdate,
)

router = APIRouter(tags=["Namespace: Compliance"])

PREFIX = "/tenants/{tenant_name}/namespaces"
RC_PREFIX = "/tenants/{tenant_name}/namespaces/{namespace_name}/retentionClasses"


# ── Compliance settings ──────────────────────────────────────────────


@router.get(PREFIX + "/{ns_name}/complianceSettings", response_model=ComplianceSettings)
async def get_compliance(
    tenant_name: str,
    ns_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/namespaces/{ns_name}/complianceSettings"
    )


@router.post(PREFIX + "/{ns_name}/complianceSettings", response_model=StatusResponse)
async def modify_compliance(
    tenant_name: str,
    ns_name: str,
    body: ComplianceSettings,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/namespaces/{ns_name}/complianceSettings",
        body=body,
    )
    return {"status": "updated"}


# ── Retention classes ────────────────────────────────────────────────


@router.get(RC_PREFIX, response_model=RetentionClassList)
async def list_retention_classes(
    tenant_name: str,
    namespace_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/namespaces/{namespace_name}/retentionClasses",
        query={"verbose": str(verbose).lower()},
    )


@router.put(RC_PREFIX, response_model=StatusResponse, status_code=201)
async def create_retention_class(
    tenant_name: str,
    namespace_name: str,
    body: RetentionClassCreate,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "PUT",
        f"/tenants/{tenant_name}/namespaces/{namespace_name}/retentionClasses",
        body=body,
    )
    return {"status": "created", "name": body.name}


@router.get(
    RC_PREFIX + "/{retention_class_name}", response_model=RetentionClassResponse
)
async def get_retention_class(
    tenant_name: str,
    namespace_name: str,
    retention_class_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/namespaces/{namespace_name}/retentionClasses/{retention_class_name}",
        query={"verbose": str(verbose).lower()},
    )


@router.head(RC_PREFIX + "/{retention_class_name}")
async def check_retention_class(
    tenant_name: str,
    namespace_name: str,
    retention_class_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    resp = await hcp.send(
        "HEAD",
        f"/tenants/{tenant_name}/namespaces/{namespace_name}/retentionClasses/{retention_class_name}",
    )
    return Response(status_code=resp.status_code)


@router.post(RC_PREFIX + "/{retention_class_name}", response_model=StatusResponse)
async def update_retention_class(
    tenant_name: str,
    namespace_name: str,
    retention_class_name: str,
    body: RetentionClassUpdate,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/namespaces/{namespace_name}/retentionClasses/{retention_class_name}",
        body=body,
    )
    return {"status": "updated"}


@router.delete(RC_PREFIX + "/{retention_class_name}", response_model=StatusResponse)
async def delete_retention_class(
    tenant_name: str,
    namespace_name: str,
    retention_class_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "DELETE",
        f"/tenants/{tenant_name}/namespaces/{namespace_name}/retentionClasses/{retention_class_name}",
    )
    return {"status": "deleted"}
