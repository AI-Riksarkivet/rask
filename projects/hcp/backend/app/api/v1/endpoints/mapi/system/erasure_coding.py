"""Erasure coding topology routes."""

from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, Query, Response

from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service
from app.schemas.erasure_coding import (
    ECLinkCandidateList,
    ECTopologyCreate,
    ECTopologyList,
    ECTopologyResponse,
    TenantCandidateList,
)
from app.schemas.common import StatusResponse

router = APIRouter(tags=["System Admin: Erasure Coding"])

EC = "/services/erasureCoding"
TOPOS = f"{EC}/ecTopologies"


# ── EC Topologies ────────────────────────────────────────────────────


@router.get(TOPOS, response_model=ECTopologyList)
async def list_ec_topologies(
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(TOPOS, query={"verbose": str(verbose).lower()})


@router.put(TOPOS, response_model=StatusResponse, status_code=201)
async def create_ec_topology(
    body: ECTopologyCreate,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("PUT", TOPOS, body=body)
    return {"status": "created", "name": body.name}


@router.get(TOPOS + "/{topology_name}", response_model=ECTopologyResponse)
async def get_ec_topology(
    topology_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"{TOPOS}/{topology_name}", query={"verbose": str(verbose).lower()}
    )


@router.head(TOPOS + "/{topology_name}")
async def check_ec_topology(
    topology_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    resp = await hcp.send("HEAD", f"{TOPOS}/{topology_name}")
    return Response(status_code=resp.status_code)


@router.post(TOPOS + "/{topology_name}", response_model=StatusResponse)
async def modify_or_retire_ec_topology(
    topology_name: str,
    retire: Optional[bool] = Query(None),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    query = {}
    if retire is not None:
        query["retire"] = ""
    await hcp.send("POST", f"{TOPOS}/{topology_name}", query=query or None)
    return {"status": "updated"}


@router.delete(TOPOS + "/{topology_name}", response_model=StatusResponse)
async def delete_ec_topology(
    topology_name: str,
    force: Optional[bool] = Query(None),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    query = {}
    if force:
        query["force"] = "true"
    await hcp.send("DELETE", f"{TOPOS}/{topology_name}", query=query or None)
    return {"status": "deleted"}


# ── EC Topology – Tenant Candidates ─────────────────────────────────


@router.get(
    TOPOS + "/{topology_name}/tenantCandidates", response_model=TenantCandidateList
)
async def get_ec_tenant_candidates(
    topology_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"{TOPOS}/{topology_name}/tenantCandidates",
        query={"verbose": str(verbose).lower()},
    )


@router.get(
    TOPOS + "/{topology_name}/tenantConflictingCandidates",
    response_model=TenantCandidateList,
)
async def get_ec_tenant_conflicting_candidates(
    topology_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"{TOPOS}/{topology_name}/tenantConflictingCandidates",
        query={"verbose": str(verbose).lower()},
    )


# ── EC Topology – Tenants ───────────────────────────────────────────


@router.get(TOPOS + "/{topology_name}/tenants", response_model=TenantCandidateList)
async def list_ec_topology_tenants(
    topology_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"{TOPOS}/{topology_name}/tenants")


@router.put(
    TOPOS + "/{topology_name}/tenants/{tenant_name}",
    response_model=StatusResponse,
    status_code=201,
)
async def add_tenant_to_ec_topology(
    topology_name: str,
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("PUT", f"{TOPOS}/{topology_name}/tenants/{tenant_name}")
    return {"status": "created", "tenant": tenant_name}


@router.delete(
    TOPOS + "/{topology_name}/tenants/{tenant_name}", response_model=StatusResponse
)
async def remove_tenant_from_ec_topology(
    topology_name: str,
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("DELETE", f"{TOPOS}/{topology_name}/tenants/{tenant_name}")
    return {"status": "deleted", "tenant": tenant_name}


# ── EC Link Candidates ──────────────────────────────────────────────


@router.get(f"{EC}/linkCandidates", response_model=ECLinkCandidateList)
async def get_ec_link_candidates(
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"{EC}/linkCandidates", query={"verbose": str(verbose).lower()}
    )
