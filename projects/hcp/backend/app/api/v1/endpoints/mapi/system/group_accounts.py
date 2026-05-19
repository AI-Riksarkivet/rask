"""System-level group account routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service
from app.schemas.group_account import GroupAccountList, GroupAccountResponse

router = APIRouter(prefix="/groupAccounts", tags=["System Admin: Identity"])


@router.get("", response_model=GroupAccountList)
async def list_system_groups(
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        "/groupAccounts", query={"verbose": str(verbose).lower()}
    )


@router.get("/{group_name}", response_model=GroupAccountResponse)
async def get_system_group(
    group_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/groupAccounts/{group_name}",
        resource=f"system group '{group_name}'",
        query={"verbose": str(verbose).lower()},
    )


@router.head("/{group_name}")
async def check_system_group(
    group_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("HEAD", f"/groupAccounts/{group_name}")
    return Response(status_code=200)
