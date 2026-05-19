"""Namespace template export endpoints."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.services.mapi_service import AuthenticatedMapiService
from app.api.dependencies import get_mapi_service

router = APIRouter(tags=["Namespace: Templates"])

PREFIX = "/tenants/{tenant_name}/namespaces"

# Properties to exclude from exported namespace data (read-only / system-generated)
_EXCLUDED_NS_KEYS = frozenset(
    {
        "id",
        "name",
        "creationTime",
        "fullyQualifiedName",
        "nameIDNA",
        "dpl",
        "effectiveDpl",
        "isDplDynamic",
        "mqeIndexingTimestamp",
        "replicationTimestamp",
        "statistics",
        "chargebackReport",
    }
)


async def _fetch_silent(
    hcp: AuthenticatedMapiService, path: str
) -> dict[str, Any] | None:
    """Fetch JSON, returning None on any error instead of raising."""
    try:
        return await hcp.fetch_json(path)
    except Exception:
        return None


async def _export_single(
    hcp: AuthenticatedMapiService, tenant_name: str, ns_name: str
) -> dict[str, Any]:
    """Export a single namespace with all its sub-resources."""
    base = f"/tenants/{tenant_name}/namespaces/{ns_name}"

    # Fetch all sub-resources in parallel
    (
        ns_data,
        versioning,
        compliance,
        permissions,
        http_proto,
        cifs_proto,
        nfs_proto,
        smtp_proto,
        indexing,
        cors,
        repl_collision,
        rc_list,
    ) = await asyncio.gather(
        _fetch_silent(hcp, f"{base}?verbose=true"),
        _fetch_silent(hcp, f"{base}/versioningSettings"),
        _fetch_silent(hcp, f"{base}/complianceSettings"),
        _fetch_silent(hcp, f"{base}/permissions"),
        _fetch_silent(hcp, f"{base}/protocols/http"),
        _fetch_silent(hcp, f"{base}/protocols/cifs"),
        _fetch_silent(hcp, f"{base}/protocols/nfs"),
        _fetch_silent(hcp, f"{base}/protocols/smtp"),
        _fetch_silent(hcp, f"{base}/customMetadataIndexingSettings"),
        _fetch_silent(hcp, f"{base}/cors"),
        _fetch_silent(hcp, f"{base}/replicationCollisionSettings"),
        _fetch_silent(hcp, f"{base}/retentionClasses"),
    )

    # Fetch individual retention class details
    retention_classes: list[dict] = []
    if rc_list:
        rc_names: list[str] = rc_list.get("name", [])
        if rc_names:
            rc_details = await asyncio.gather(
                *[
                    _fetch_silent(hcp, f"{base}/retentionClasses/{rc}")
                    for rc in rc_names
                ]
            )
            retention_classes = [d for d in rc_details if d is not None]

    # Build the core config, excluding read-only fields
    config: dict[str, Any] = {"name": ns_name}
    if ns_data:
        for key, value in ns_data.items():
            if key not in _EXCLUDED_NS_KEYS:
                config[key] = value

    # Attach sub-resources
    if versioning:
        config["versioning"] = versioning
    if compliance:
        config["compliance"] = compliance
    if permissions:
        config["permissions"] = permissions

    protocols: dict[str, Any] = {}
    if http_proto:
        protocols["http"] = http_proto
    if cifs_proto:
        protocols["cifs"] = cifs_proto
    if nfs_proto:
        protocols["nfs"] = nfs_proto
    if smtp_proto:
        protocols["smtp"] = smtp_proto
    if protocols:
        config["protocols"] = protocols

    if retention_classes:
        config["retentionClasses"] = retention_classes
    if indexing:
        config["indexing"] = indexing
    if cors:
        config["cors"] = cors
    if repl_collision:
        config["replicationCollision"] = repl_collision

    return config


@router.get(PREFIX + "/{ns_name}/export")
async def export_namespace(
    tenant_name: str,
    ns_name: str,
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
) -> dict[str, Any]:
    """Export a single namespace configuration as a template."""
    config = await _export_single(hcp, tenant_name, ns_name)
    return {
        "version": "1.0",
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "sourceTenant": tenant_name,
        "namespaces": [config],
    }


@router.get(PREFIX + "/export")
async def export_namespaces(
    tenant_name: str,
    names: str = Query(..., description="Comma-separated namespace names"),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
) -> dict[str, Any]:
    """Export multiple namespace configurations as a template bundle."""
    ns_names = [n.strip() for n in names.split(",") if n.strip()]
    configs = await asyncio.gather(
        *[_export_single(hcp, tenant_name, ns) for ns in ns_names]
    )
    return {
        "version": "1.0",
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "sourceTenant": tenant_name,
        "namespaces": list(configs),
    }
