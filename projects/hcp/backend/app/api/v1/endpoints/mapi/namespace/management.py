"""Namespace CRUD and versioning routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service
from app.schemas.namespace import (
    NamespaceCreate,
    NamespaceUpdate,
    NamespaceResponse,
    NamespaceList,
)
from app.schemas.common import ListQueryParams, VersioningSettings, StatusResponse

router = APIRouter(tags=["Namespace: Management"])

PREFIX = "/tenants/{tenant_name}/namespaces"


# ── Namespace list & create ──────────────────────────────────────────


@router.get(PREFIX, response_model=NamespaceList)
async def list_namespaces(
    tenant_name: str,
    qp: ListQueryParams = Depends(),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/namespaces",
        resource="namespaces",
        query=qp.to_query(),
    )


@router.put(PREFIX, response_model=StatusResponse, status_code=201)
async def create_namespace(
    tenant_name: str,
    body: NamespaceCreate,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "PUT",
        f"/tenants/{tenant_name}/namespaces",
        resource="namespace",
        body=body,
    )
    return {"status": "created", "name": body.name}


# ── Single namespace ─────────────────────────────────────────────────


@router.get(PREFIX + "/{ns_name}", response_model=NamespaceResponse)
async def get_namespace(
    tenant_name: str,
    ns_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/namespaces/{ns_name}",
        resource=f"namespace '{ns_name}'",
        query={"verbose": str(verbose).lower()},
    )


@router.head(PREFIX + "/{ns_name}")
async def check_namespace(
    tenant_name: str,
    ns_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "HEAD",
        f"/tenants/{tenant_name}/namespaces/{ns_name}",
        resource=f"namespace '{ns_name}'",
    )
    return Response(status_code=200)


@router.post(PREFIX + "/{ns_name}", response_model=StatusResponse)
async def modify_namespace(
    tenant_name: str,
    ns_name: str,
    body: NamespaceUpdate,
    resetMQECheckpoint: str | None = Query(None),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    q = {}
    if resetMQECheckpoint is not None:
        q["resetMQECheckpoint"] = resetMQECheckpoint
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/namespaces/{ns_name}",
        resource=f"namespace '{ns_name}'",
        body=body,
        query=q or None,
    )
    return {"status": "updated"}


@router.delete(PREFIX + "/{ns_name}", response_model=StatusResponse)
async def delete_namespace(
    tenant_name: str,
    ns_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "DELETE",
        f"/tenants/{tenant_name}/namespaces/{ns_name}",
        resource=f"namespace '{ns_name}'",
    )
    return {"status": "deleted"}


# ── Versioning settings ─────────────────────────────────────────────


@router.get(PREFIX + "/{ns_name}/versioningSettings", response_model=VersioningSettings)
async def get_versioning(
    tenant_name: str,
    ns_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/namespaces/{ns_name}/versioningSettings"
    )


@router.post(PREFIX + "/{ns_name}/versioningSettings", response_model=StatusResponse)
async def modify_versioning(
    tenant_name: str,
    ns_name: str,
    body: VersioningSettings,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/namespaces/{ns_name}/versioningSettings",
        body=body,
    )
    return {"status": "updated"}


@router.delete(PREFIX + "/{ns_name}/versioningSettings", response_model=StatusResponse)
async def delete_versioning(
    tenant_name: str,
    ns_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "DELETE",
        f"/tenants/{tenant_name}/namespaces/{ns_name}/versioningSettings",
    )
    return {"status": "deleted"}
