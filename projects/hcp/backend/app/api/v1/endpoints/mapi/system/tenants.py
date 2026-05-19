"""System-level tenant routes (list & create)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service
from app.schemas.tenant import TenantCreate, TenantList
from app.schemas.common import StatusResponse

router = APIRouter(prefix="/tenants", tags=["System Admin: Tenants"])


@router.get("", response_model=TenantList)
async def list_tenants(
    verbose: bool = False,
    prettyprint: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        "/tenants",
        resource="tenants",
        query={"verbose": str(verbose).lower(), "prettyprint": prettyprint or None},
    )


@router.put("", response_model=StatusResponse, status_code=201)
async def create_tenant(
    body: TenantCreate,
    username: str = Query(...),
    password: str = Query(...),
    forcePasswordChange: bool = Query(False),
    initialSecurityGroup: str | None = Query(None),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    """Create an HCP tenant (system-level)."""
    q: dict = {
        "username": username,
        "password": password,
        "forcePasswordChange": str(forcePasswordChange).lower(),
    }
    if initialSecurityGroup is not None:
        q["initialSecurityGroup"] = initialSecurityGroup
    await hcp.send("PUT", "/tenants", resource="tenant", body=body, query=q)
    return {"status": "created", "name": body.name}
