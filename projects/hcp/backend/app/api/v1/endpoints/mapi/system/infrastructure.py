"""System infrastructure routes – network, licenses, statistics."""

from __future__ import annotations

from fastapi import APIRouter, Depends, UploadFile, File

from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service
from app.schemas.license import License, LicenseList
from app.schemas.network import NetworkSettings
from app.schemas.statistics import NodeStatistics, ServiceStatistics
from app.schemas.common import StatusResponse

router = APIRouter(tags=["System Admin: Infrastructure"])


# ── Network ──────────────────────────────────────────────────────────


@router.get("/network", response_model=NetworkSettings)
async def get_network_settings(
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json("/network", query={"verbose": str(verbose).lower()})


@router.post("/network", response_model=StatusResponse)
async def modify_network_settings(
    body: NetworkSettings,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("POST", "/network", body=body)
    return {"status": "updated"}


# ── Licenses ─────────────────────────────────────────────────────────


@router.get("/storage/licenses", response_model=LicenseList)
async def list_licenses(
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        "/storage/licenses", query={"verbose": str(verbose).lower()}
    )


@router.put("/storage/licenses", response_model=StatusResponse, status_code=201)
async def upload_license(
    file: UploadFile = File(...),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    content = await file.read()
    await hcp.send("PUT", "/storage/licenses", body=content)
    return {"status": "created"}


@router.get("/storage/licenses/{serial_number}", response_model=License)
async def get_license(
    serial_number: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"/storage/licenses/{serial_number}")


# ── Statistics ───────────────────────────────────────────────────────


@router.get("/nodes/statistics", response_model=NodeStatistics)
async def get_node_statistics(
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        "/nodes/statistics", query={"verbose": str(verbose).lower()}
    )


@router.get("/services/statistics", response_model=ServiceStatistics)
async def get_service_statistics(
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        "/services/statistics", query={"verbose": str(verbose).lower()}
    )
