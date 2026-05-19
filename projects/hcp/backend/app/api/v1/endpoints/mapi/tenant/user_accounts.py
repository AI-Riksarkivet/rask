"""Tenant-level user account routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service
from app.schemas.user_account import (
    UserAccountCreate,
    UserAccountUpdate,
    UpdatePasswordRequest,
    UserAccountList,
    UserAccountResponse,
)
from app.schemas.common import ListQueryParams, DataAccessPermissions, StatusResponse

router = APIRouter(tags=["Tenant Admin: Identity"])

T_PREFIX = "/tenants/{tenant_name}/userAccounts"


@router.get(T_PREFIX, response_model=UserAccountList | list[UserAccountResponse])
async def list_user_accounts(
    tenant_name: str,
    qp: ListQueryParams = Depends(),
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/userAccounts",
        query=qp.to_query(verbose=str(verbose).lower()),
    )


@router.put(T_PREFIX, response_model=StatusResponse, status_code=201)
async def create_user_account(
    tenant_name: str,
    body: UserAccountCreate,
    password: str = Query(...),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "PUT",
        f"/tenants/{tenant_name}/userAccounts",
        resource="user account",
        body=body,
        query={"password": password},
    )
    return {"status": "created", "username": body.username}


@router.post(T_PREFIX, response_model=StatusResponse)
async def reset_passwords(
    tenant_name: str,
    resetPasswords: str = Query(...),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/userAccounts",
        body={},
        query={"resetPasswords": resetPasswords},
    )
    return {"status": "passwords_reset"}


@router.get(T_PREFIX + "/{username}", response_model=UserAccountResponse)
async def get_user_account(
    tenant_name: str,
    username: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/userAccounts/{username}",
        resource=f"user '{username}'",
        query={"verbose": str(verbose).lower()},
    )


@router.head(T_PREFIX + "/{username}")
async def check_user_account(
    tenant_name: str,
    username: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "HEAD",
        f"/tenants/{tenant_name}/userAccounts/{username}",
        resource=f"user '{username}'",
    )
    return Response(status_code=200)


@router.post(T_PREFIX + "/{username}", response_model=StatusResponse)
async def modify_user_account(
    tenant_name: str,
    username: str,
    body: UserAccountUpdate,
    password: str | None = Query(None),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    q = {}
    if password:
        q["password"] = password
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/userAccounts/{username}",
        resource=f"user '{username}'",
        body=body,
        query=q or None,
    )
    return {"status": "updated"}


@router.delete(T_PREFIX + "/{username}", response_model=StatusResponse)
async def delete_user_account(
    tenant_name: str,
    username: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "DELETE",
        f"/tenants/{tenant_name}/userAccounts/{username}",
        resource=f"user '{username}'",
    )
    return {"status": "deleted"}


# ── Change password ──────────────────────────────────────────────────


@router.post(T_PREFIX + "/{username}/changePassword", response_model=StatusResponse)
async def change_password(
    tenant_name: str,
    username: str,
    body: UpdatePasswordRequest,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/userAccounts/{username}/changePassword",
        body=body,
    )
    return {"status": "password_changed"}


# ── Data access permissions ──────────────────────────────────────────


@router.get(
    T_PREFIX + "/{username}/dataAccessPermissions", response_model=DataAccessPermissions
)
async def get_user_data_perms(
    tenant_name: str,
    username: str,
    qp: ListQueryParams = Depends(),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/userAccounts/{username}/dataAccessPermissions",
        query=qp.to_query(),
    )


@router.post(
    T_PREFIX + "/{username}/dataAccessPermissions", response_model=StatusResponse
)
async def modify_user_data_perms(
    tenant_name: str,
    username: str,
    body: DataAccessPermissions,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/userAccounts/{username}/dataAccessPermissions",
        body=body,
    )
    return {"status": "updated"}
