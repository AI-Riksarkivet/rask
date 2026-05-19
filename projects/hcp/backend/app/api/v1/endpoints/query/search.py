"""Metadata Query API endpoints — object and operation searches."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_query_service
from app.schemas.query import (
    ObjectQuery,
    ObjectQueryResponse,
    OperationQuery,
    OperationQueryResponse,
)
from app.services.query_service import AuthenticatedQueryService

router = APIRouter(prefix="/tenants", tags=["Metadata Query"])


@router.post(
    "/{tenant_name}/objects",
    response_model=ObjectQueryResponse,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def query_objects(
    tenant_name: str,
    body: ObjectQuery,
    svc: AuthenticatedQueryService = Depends(get_query_service),
):
    """Search objects by metadata, paths, size, retention, and custom metadata."""
    return await svc.object_query(tenant_name, body)


@router.post(
    "/{tenant_name}/operations",
    response_model=OperationQueryResponse,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def query_operations(
    tenant_name: str,
    body: OperationQuery,
    svc: AuthenticatedQueryService = Depends(get_query_service),
):
    """Audit trail of create/delete/purge events."""
    return await svc.operation_query(tenant_name, body)
