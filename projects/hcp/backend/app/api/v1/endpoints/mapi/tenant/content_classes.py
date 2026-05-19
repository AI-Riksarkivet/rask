"""Content class routes for tenants."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service
from app.schemas.content_class import (
    ContentClassCreate,
    ContentClassUpdate,
    ContentClassList,
    ContentClassResponse,
)
from app.schemas.common import StatusResponse

router = APIRouter(tags=["Tenant Admin: Content Classes"])

PREFIX = "/tenants/{tenant_name}/contentClasses"


@router.get(PREFIX, response_model=ContentClassList | list[ContentClassResponse])
async def list_content_classes(
    tenant_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/contentClasses",
        query={"verbose": str(verbose).lower()},
    )


@router.put(PREFIX, response_model=StatusResponse, status_code=201)
async def create_content_class(
    tenant_name: str,
    body: ContentClassCreate,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "PUT",
        f"/tenants/{tenant_name}/contentClasses",
        body=body,
    )
    return {"status": "created", "name": body.name}


@router.get(PREFIX + "/{content_class_name}", response_model=ContentClassResponse)
async def get_content_class(
    tenant_name: str,
    content_class_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/contentClasses/{content_class_name}",
        query={"verbose": str(verbose).lower()},
    )


@router.head(PREFIX + "/{content_class_name}")
async def check_content_class(
    tenant_name: str,
    content_class_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    resp = await hcp.send(
        "HEAD",
        f"/tenants/{tenant_name}/contentClasses/{content_class_name}",
    )
    return Response(status_code=resp.status_code)


@router.post(PREFIX + "/{content_class_name}", response_model=StatusResponse)
async def update_content_class(
    tenant_name: str,
    content_class_name: str,
    body: ContentClassUpdate,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/contentClasses/{content_class_name}",
        body=body,
    )
    return {"status": "updated"}


@router.delete(PREFIX + "/{content_class_name}", response_model=StatusResponse)
async def delete_content_class(
    tenant_name: str,
    content_class_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "DELETE",
        f"/tenants/{tenant_name}/contentClasses/{content_class_name}",
    )
    return {"status": "deleted"}
