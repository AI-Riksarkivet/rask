"""Tenant-level statistics and chargeback routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service
from app.schemas.common import ChargebackParams
from app.schemas.statistics import NamespaceStatistics
from app.schemas.tenant import ChargebackReport

router = APIRouter(prefix="/tenants", tags=["Tenant Admin: Statistics"])


@router.get("/{tenant_name}/chargebackReport", response_model=ChargebackReport)
async def get_tenant_chargeback(
    tenant_name: str,
    params: ChargebackParams = Depends(),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    q = {}
    if params.start:
        q["start"] = params.start
    if params.end:
        q["end"] = params.end
    if params.granularity:
        q["granularity"] = params.granularity
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/chargebackReport",
        query=q,
    )


@router.get("/{tenant_name}/statistics", response_model=NamespaceStatistics)
async def get_tenant_statistics(
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"/tenants/{tenant_name}/statistics")
