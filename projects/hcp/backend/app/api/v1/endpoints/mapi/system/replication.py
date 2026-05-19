"""Replication routes – links, certificates, schedules, content, service."""

from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, Query, Response, UploadFile, File

from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service
from app.schemas.replication import (
    Certificate,
    CertificateList,
    LinkContent,
    LinkCreate,
    LinkList,
    LinkResponse,
    LinkUpdate,
    Schedule,
    ReplicationService,
)
from app.schemas.common import StatusResponse

router = APIRouter(tags=["System Admin: Replication"])


# ── Replication Service ──────────────────────────────────────────────


@router.get("/services/replication", response_model=ReplicationService)
async def get_replication_service(
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        "/services/replication", query={"verbose": str(verbose).lower()}
    )


@router.post("/services/replication", response_model=StatusResponse)
async def modify_replication_service(
    body: Optional[ReplicationService] = None,
    shutDownAllLinks: Optional[str] = Query(None),
    reestablishAllLinks: Optional[bool] = Query(None),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    query = {}
    if shutDownAllLinks is not None:
        query["shutDownAllLinks"] = shutDownAllLinks
    if reestablishAllLinks is not None:
        query["reestablishAllLinks"] = ""
    await hcp.send("POST", "/services/replication", body=body, query=query or None)
    return {"status": "updated"}


# ── Certificates ─────────────────────────────────────────────────────


@router.get("/services/replication/certificates", response_model=CertificateList)
async def list_certificates(
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        "/services/replication/certificates",
        query={"verbose": str(verbose).lower()},
    )


@router.put(
    "/services/replication/certificates", response_model=StatusResponse, status_code=201
)
async def upload_certificate(
    file: UploadFile = File(...),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    content = await file.read()
    await hcp.send("PUT", "/services/replication/certificates", body=content)
    return {"status": "created"}


@router.get(
    "/services/replication/certificates/{certificate_id}", response_model=Certificate
)
async def get_certificate(
    certificate_id: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"/services/replication/certificates/{certificate_id}")


@router.delete(
    "/services/replication/certificates/{certificate_id}", response_model=StatusResponse
)
async def delete_certificate(
    certificate_id: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("DELETE", f"/services/replication/certificates/{certificate_id}")
    return {"status": "deleted", "certificateId": certificate_id}


@router.get("/services/replication/certificates/server")
async def download_server_certificate(
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    resp = await hcp.send("GET", "/services/replication/certificates/server")
    return Response(content=resp.text, media_type="text/plain")


# ── Links ────────────────────────────────────────────────────────────

LINKS = "/services/replication/links"


@router.get(LINKS, response_model=LinkList)
async def list_links(
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(LINKS, query={"verbose": str(verbose).lower()})


@router.put(LINKS, response_model=StatusResponse, status_code=201)
async def create_link(
    body: LinkCreate,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("PUT", LINKS, body=body)
    return {"status": "created", "name": body.name}


@router.get(LINKS + "/{link_name}", response_model=LinkResponse)
async def get_link(
    link_name: str,
    verbose: bool = False,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"{LINKS}/{link_name}", query={"verbose": str(verbose).lower()}
    )


@router.head(LINKS + "/{link_name}")
async def check_link(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    resp = await hcp.send("HEAD", f"{LINKS}/{link_name}")
    return Response(status_code=resp.status_code)


@router.post(LINKS + "/{link_name}", response_model=StatusResponse)
async def modify_or_action_link(
    link_name: str,
    body: Optional[LinkUpdate] = None,
    suspend: Optional[bool] = Query(None),
    resume: Optional[bool] = Query(None),
    failOver: Optional[bool] = Query(None),
    failBack: Optional[bool] = Query(None),
    beginRecover: Optional[bool] = Query(None),
    completeRecovery: Optional[bool] = Query(None),
    restore: Optional[bool] = Query(None),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    query = {}
    for action_name, action_val in [
        ("suspend", suspend),
        ("resume", resume),
        ("failOver", failOver),
        ("failBack", failBack),
        ("beginRecover", beginRecover),
        ("completeRecovery", completeRecovery),
        ("restore", restore),
    ]:
        if action_val is not None:
            query[action_name] = ""
    await hcp.send("POST", f"{LINKS}/{link_name}", body=body, query=query or None)
    return {"status": "updated"}


@router.delete(LINKS + "/{link_name}", response_model=StatusResponse)
async def delete_link(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("DELETE", f"{LINKS}/{link_name}")
    return {"status": "deleted", "name": link_name}


# ── Link Content ─────────────────────────────────────────────────────


@router.get(LINKS + "/{link_name}/content", response_model=LinkContent)
async def get_link_content(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"{LINKS}/{link_name}/content")


# ── Link Content – Tenants ───────────────────────────────────────────


@router.get(LINKS + "/{link_name}/content/tenants", response_model=LinkContent)
async def list_link_tenants(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"{LINKS}/{link_name}/content/tenants")


@router.put(
    LINKS + "/{link_name}/content/tenants/{tenant_name}",
    response_model=StatusResponse,
    status_code=201,
)
async def add_tenant_to_link(
    link_name: str,
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("PUT", f"{LINKS}/{link_name}/content/tenants/{tenant_name}")
    return {"status": "created", "tenant": tenant_name}


@router.get(
    LINKS + "/{link_name}/content/tenants/{tenant_name}", response_model=LinkContent
)
async def get_link_tenant(
    link_name: str,
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"{LINKS}/{link_name}/content/tenants/{tenant_name}")


@router.post(
    LINKS + "/{link_name}/content/tenants/{tenant_name}", response_model=StatusResponse
)
async def action_link_tenant(
    link_name: str,
    tenant_name: str,
    pause: Optional[bool] = Query(None),
    resume: Optional[bool] = Query(None),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    query = {}
    if pause is not None:
        query["pause"] = ""
    if resume is not None:
        query["resume"] = ""
    await hcp.send(
        "POST",
        f"{LINKS}/{link_name}/content/tenants/{tenant_name}",
        query=query or None,
    )
    return {"status": "updated"}


@router.delete(
    LINKS + "/{link_name}/content/tenants/{tenant_name}", response_model=StatusResponse
)
async def remove_tenant_from_link(
    link_name: str,
    tenant_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("DELETE", f"{LINKS}/{link_name}/content/tenants/{tenant_name}")
    return {"status": "deleted", "tenant": tenant_name}


# ── Link Content – Default Namespace Directories ─────────────────────


@router.get(
    LINKS + "/{link_name}/content/defaultNamespaceDirectories",
    response_model=LinkContent,
)
async def list_link_default_ns_dirs(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"{LINKS}/{link_name}/content/defaultNamespaceDirectories"
    )


@router.put(
    LINKS + "/{link_name}/content/defaultNamespaceDirectories/{dir_name}",
    response_model=StatusResponse,
    status_code=201,
)
async def add_default_ns_dir_to_link(
    link_name: str,
    dir_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "PUT",
        f"{LINKS}/{link_name}/content/defaultNamespaceDirectories/{dir_name}",
    )
    return {"status": "created", "directory": dir_name}


@router.delete(
    LINKS + "/{link_name}/content/defaultNamespaceDirectories/{dir_name}",
    response_model=StatusResponse,
)
async def remove_default_ns_dir_from_link(
    link_name: str,
    dir_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "DELETE",
        f"{LINKS}/{link_name}/content/defaultNamespaceDirectories/{dir_name}",
    )
    return {"status": "deleted", "directory": dir_name}


# ── Link Content – Chained Links ─────────────────────────────────────


@router.get(LINKS + "/{link_name}/content/chainedLinks", response_model=LinkContent)
async def list_chained_links(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"{LINKS}/{link_name}/content/chainedLinks")


@router.put(
    LINKS + "/{link_name}/content/chainedLinks/{chained_link_name}",
    response_model=StatusResponse,
    status_code=201,
)
async def add_chained_link(
    link_name: str,
    chained_link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "PUT", f"{LINKS}/{link_name}/content/chainedLinks/{chained_link_name}"
    )
    return {"status": "created", "name": chained_link_name}


@router.delete(
    LINKS + "/{link_name}/content/chainedLinks/{chained_link_name}",
    response_model=StatusResponse,
)
async def remove_chained_link(
    link_name: str,
    chained_link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send(
        "DELETE", f"{LINKS}/{link_name}/content/chainedLinks/{chained_link_name}"
    )
    return {"status": "deleted", "name": chained_link_name}


# ── Link Candidates ──────────────────────────────────────────────────


@router.get(LINKS + "/{link_name}/localCandidates", response_model=LinkContent)
async def get_local_candidates(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"{LINKS}/{link_name}/localCandidates")


@router.get(LINKS + "/{link_name}/localCandidates/tenants", response_model=LinkContent)
async def get_local_candidate_tenants(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"{LINKS}/{link_name}/localCandidates/tenants")


@router.get(
    LINKS + "/{link_name}/localCandidates/defaultNamespaceDirectories",
    response_model=LinkContent,
)
async def get_local_candidate_dirs(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"{LINKS}/{link_name}/localCandidates/defaultNamespaceDirectories"
    )


@router.get(
    LINKS + "/{link_name}/localCandidates/chainedLinks", response_model=LinkContent
)
async def get_local_candidate_chained(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"{LINKS}/{link_name}/localCandidates/chainedLinks")


@router.get(LINKS + "/{link_name}/remoteCandidates", response_model=LinkContent)
async def get_remote_candidates(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"{LINKS}/{link_name}/remoteCandidates")


@router.get(LINKS + "/{link_name}/remoteCandidates/tenants", response_model=LinkContent)
async def get_remote_candidate_tenants(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"{LINKS}/{link_name}/remoteCandidates/tenants")


@router.get(
    LINKS + "/{link_name}/remoteCandidates/defaultNamespaceDirectories",
    response_model=LinkContent,
)
async def get_remote_candidate_dirs(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(
        f"{LINKS}/{link_name}/remoteCandidates/defaultNamespaceDirectories"
    )


@router.get(
    LINKS + "/{link_name}/remoteCandidates/chainedLinks", response_model=LinkContent
)
async def get_remote_candidate_chained(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"{LINKS}/{link_name}/remoteCandidates/chainedLinks")


# ── Link Schedule ────────────────────────────────────────────────────


@router.get(LINKS + "/{link_name}/schedule", response_model=Schedule)
async def get_link_schedule(
    link_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    return await hcp.fetch_json(f"{LINKS}/{link_name}/schedule")


@router.post(LINKS + "/{link_name}/schedule", response_model=StatusResponse)
async def set_link_schedule(
    link_name: str,
    body: Schedule,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
):
    await hcp.send("POST", f"{LINKS}/{link_name}/schedule", body=body)
    return {"status": "updated"}
