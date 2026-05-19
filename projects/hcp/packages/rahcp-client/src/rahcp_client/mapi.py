"""MapiOps — tenant admin and namespace CRUD only."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rahcp_client.client import HCPClient

log = logging.getLogger(__name__)


class MapiOps:
    """MAPI management-plane — tenant admin and namespace operations only.

    This is intentionally thin: user/group CRUD, statistics, chargeback,
    and system-level admin are handled by the backend directly.

    HTTP method mapping (matches HCP MAPI conventions):
        GET  — read
        PUT  — create
        POST — update/modify
        DELETE — delete
    """

    def __init__(self, client: HCPClient) -> None:
        self._client = client

    async def list_namespaces(
        self, tenant: str, *, verbose: bool = False
    ) -> list[dict[str, Any]]:
        """List all namespaces for a tenant."""
        params: dict[str, Any] = {}
        if verbose:
            params["verbose"] = "true"
        resp = await self._client.request(
            "GET", f"/mapi/tenants/{tenant}/namespaces", params=params
        )
        return resp.json()

    async def get_namespace(
        self, tenant: str, ns: str, *, verbose: bool = False
    ) -> dict[str, Any]:
        """Get namespace details."""
        params: dict[str, Any] = {}
        if verbose:
            params["verbose"] = "true"
        resp = await self._client.request(
            "GET", f"/mapi/tenants/{tenant}/namespaces/{ns}", params=params
        )
        return resp.json()

    async def create_namespace(
        self, tenant: str, ns_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a namespace. Uses PUT (MAPI convention)."""
        resp = await self._client.request(
            "PUT", f"/mapi/tenants/{tenant}/namespaces", json=ns_data
        )
        return resp.json()

    async def update_namespace(
        self, tenant: str, ns: str, data: dict[str, Any]
    ) -> None:
        """Update namespace settings. Uses POST (MAPI convention)."""
        await self._client.request(
            "POST", f"/mapi/tenants/{tenant}/namespaces/{ns}", json=data
        )

    async def delete_namespace(self, tenant: str, ns: str) -> None:
        """Delete a namespace."""
        await self._client.request("DELETE", f"/mapi/tenants/{tenant}/namespaces/{ns}")

    async def export_namespace(self, tenant: str, ns: str) -> dict[str, Any]:
        """Export a namespace as a reusable template."""
        resp = await self._client.request(
            "GET", f"/mapi/tenants/{tenant}/namespaces/{ns}/export"
        )
        return resp.json()

    async def export_namespaces(self, tenant: str, names: list[str]) -> dict[str, Any]:
        """Export multiple namespaces as templates."""
        resp = await self._client.request(
            "GET",
            f"/mapi/tenants/{tenant}/namespaces/export",
            params={"names": ",".join(names)},
        )
        return resp.json()
