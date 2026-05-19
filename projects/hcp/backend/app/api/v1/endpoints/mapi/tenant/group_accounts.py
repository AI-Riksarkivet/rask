"""Tenant-level group account routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service
from app.schemas.group_account import (
    GroupAccountCreate,
    GroupAccountUpdate,
    GroupAccountList,
    GroupAccountResponse,
)
from app.schemas.common import DataAccessPermissions, StatusResponse

router = APIRouter(tags=["Tenant Admin: Identity"])

T_PREFIX = "/tenants/{tenant_name}/groupAccounts"


@router.get(T_PREFIX, response_model=GroupAccountList | list[GroupAccountResponse])
async def list_group_accounts(
    tenant_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/groupAccounts",
        query={"verbose": str(verbose).lower()},
    )


@router.put(T_PREFIX, response_model=StatusResponse, status_code=201)
async def create_group_account(
    tenant_name: str,
    body: GroupAccountCreate,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "PUT",
        f"/tenants/{tenant_name}/groupAccounts",
        resource="group account",
        body=body,
    )
    return {"status": "created", "groupname": body.groupname}


@router.get(T_PREFIX + "/{group_name}", response_model=GroupAccountResponse)
async def get_group_account(
    tenant_name: str,
    group_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/groupAccounts/{group_name}",
        resource=f"group '{group_name}'",
        query={"verbose": str(verbose).lower()},
    )


@router.head(T_PREFIX + "/{group_name}")
async def check_group_account(
    tenant_name: str,
    group_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("HEAD", f"/tenants/{tenant_name}/groupAccounts/{group_name}")
    return Response(status_code=200)


@router.post(T_PREFIX + "/{group_name}", response_model=StatusResponse)
async def modify_group_account(
    tenant_name: str,
    group_name: str,
    body: GroupAccountUpdate,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/groupAccounts/{group_name}",
        body=body,
    )
    return {"status": "updated"}


@router.delete(T_PREFIX + "/{group_name}", response_model=StatusResponse)
async def delete_group_account(
    tenant_name: str,
    group_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("DELETE", f"/tenants/{tenant_name}/groupAccounts/{group_name}")
    return {"status": "deleted"}


# ── Data access permissions ──────────────────────────────────────────


@router.get(
    T_PREFIX + "/{group_name}/dataAccessPermissions",
    response_model=DataAccessPermissions,
)
async def get_group_data_perms(
    tenant_name: str,
    group_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/groupAccounts/{group_name}/dataAccessPermissions"
    )


@router.post(
    T_PREFIX + "/{group_name}/dataAccessPermissions", response_model=StatusResponse
)
async def modify_group_data_perms(
    tenant_name: str,
    group_name: str,
    body: DataAccessPermissions,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/groupAccounts/{group_name}/dataAccessPermissions",
        body=body,
    )
    return {"status": "updated"}
