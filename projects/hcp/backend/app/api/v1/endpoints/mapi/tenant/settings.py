"""Tenant-level settings routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.api.errors import parse_json_response, raise_for_hcp_status
from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service
from app.schemas.tenant import (
    TenantUpdate,
    TenantResponse,
    ConsoleSecurity,
    ContactInfo,
    EmailNotification,
    SearchSecurity,
    AvailableServicePlan,
    AvailableServicePlanList,
)
from app.schemas.namespace import NamespaceDefaults, CorsConfiguration
from app.schemas.common import StatusResponse, PermissionsResponse, dump_for_hcp

router = APIRouter(prefix="/tenants", tags=["Tenant Admin: Settings"])


# ── Single tenant ────────────────────────────────────────────────────


@router.get("/{tenant_name}", response_model=TenantResponse)
async def get_tenant(
    tenant_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    resp = await hcp.get(
        f"/tenants/{tenant_name}",
        query={"verbose": str(verbose).lower()},
    )
    raise_for_hcp_status(resp, f"tenant '{tenant_name}'")
    data = parse_json_response(resp)
    version = resp.headers.get("X-HCP-SoftwareVersion")
    if version:
        data["softwareVersion"] = version
    return data


@router.head("/{tenant_name}")
async def check_tenant(
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "HEAD", f"/tenants/{tenant_name}", resource=f"tenant '{tenant_name}'"
    )
    return Response(status_code=200)


@router.post("/{tenant_name}", response_model=StatusResponse)
async def modify_tenant(
    tenant_name: str,
    body: TenantUpdate,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}",
        resource=f"tenant '{tenant_name}'",
        body=body,
    )
    return {"status": "updated"}


# ── Console security ─────────────────────────────────────────────────


@router.get("/{tenant_name}/consoleSecurity", response_model=ConsoleSecurity)
async def get_console_security(
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"/tenants/{tenant_name}/consoleSecurity")


@router.post("/{tenant_name}/consoleSecurity", response_model=StatusResponse)
async def modify_console_security(
    tenant_name: str,
    body: ConsoleSecurity,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/consoleSecurity",
        body=dump_for_hcp(body),
    )
    return {"status": "updated"}


# ── Contact info ─────────────────────────────────────────────────────


@router.get("/{tenant_name}/contactInfo", response_model=ContactInfo)
async def get_contact_info(
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"/tenants/{tenant_name}/contactInfo")


@router.post("/{tenant_name}/contactInfo", response_model=StatusResponse)
async def modify_contact_info(
    tenant_name: str,
    body: ContactInfo,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("POST", f"/tenants/{tenant_name}/contactInfo", body=body)
    return {"status": "updated"}


# ── Email notification ───────────────────────────────────────────────


@router.get("/{tenant_name}/emailNotification", response_model=EmailNotification)
async def get_email_notification(
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"/tenants/{tenant_name}/emailNotification")


@router.post("/{tenant_name}/emailNotification", response_model=StatusResponse)
async def modify_email_notification(
    tenant_name: str,
    body: EmailNotification,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("POST", f"/tenants/{tenant_name}/emailNotification", body=body)
    return {"status": "updated"}


# ── Namespace defaults ───────────────────────────────────────────────


@router.get("/{tenant_name}/namespaceDefaults", response_model=NamespaceDefaults)
async def get_namespace_defaults(
    tenant_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/namespaceDefaults",
        query={"verbose": str(verbose).lower()},
    )


@router.post("/{tenant_name}/namespaceDefaults", response_model=StatusResponse)
async def modify_namespace_defaults(
    tenant_name: str,
    body: NamespaceDefaults,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("POST", f"/tenants/{tenant_name}/namespaceDefaults", body=body)
    return {"status": "updated"}


# ── Search security ──────────────────────────────────────────────────


@router.get("/{tenant_name}/searchSecurity", response_model=SearchSecurity)
async def get_search_security(
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"/tenants/{tenant_name}/searchSecurity")


@router.post("/{tenant_name}/searchSecurity", response_model=StatusResponse)
async def modify_search_security(
    tenant_name: str,
    body: SearchSecurity,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "POST",
        f"/tenants/{tenant_name}/searchSecurity",
        body=dump_for_hcp(body),
    )
    return {"status": "updated"}


# ── Tenant permissions ───────────────────────────────────────────────


@router.get("/{tenant_name}/permissions", response_model=PermissionsResponse)
async def get_tenant_permissions(
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"/tenants/{tenant_name}/permissions")


@router.post("/{tenant_name}/permissions", response_model=StatusResponse)
async def modify_tenant_permissions(
    tenant_name: str,
    body: PermissionsResponse,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("POST", f"/tenants/{tenant_name}/permissions", body=body)
    return {"status": "updated"}


# ── Available service plans ──────────────────────────────────────────


@router.get(
    "/{tenant_name}/availableServicePlans", response_model=AvailableServicePlanList
)
async def list_available_service_plans(
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"/tenants/{tenant_name}/availableServicePlans")


@router.get(
    "/{tenant_name}/availableServicePlans/{plan_name}",
    response_model=AvailableServicePlan,
)
async def get_available_service_plan(
    tenant_name: str,
    plan_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"/tenants/{tenant_name}/availableServicePlans/{plan_name}"
    )


# ── Tenant CORS ──────────────────────────────────────────────────────


@router.get("/{tenant_name}/cors", response_model=CorsConfiguration)
async def get_tenant_cors(
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"/tenants/{tenant_name}/cors")


@router.put("/{tenant_name}/cors", response_model=StatusResponse, status_code=201)
async def set_tenant_cors(
    tenant_name: str,
    body: CorsConfiguration,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("PUT", f"/tenants/{tenant_name}/cors", body=body)
    return {"status": "created", "tenant": tenant_name}


@router.delete("/{tenant_name}/cors", response_model=StatusResponse)
async def delete_tenant_cors(
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("DELETE", f"/tenants/{tenant_name}/cors")
    return {"status": "deleted"}
