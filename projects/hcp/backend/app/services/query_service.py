"""HCP Metadata Query API HTTP client.

Handles POST requests to the tenant query endpoint for object and
operation metadata searches.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.core.auth_utils import get_hcp_auth_header
from app.core.config import MapiSettings
from app.core.tenant_routing import query_url_for_tenant
from app.schemas.query import (
    ObjectQuery,
    ObjectQueryRequest,
    ObjectQueryResponse,
    OperationQuery,
    OperationQueryRequest,
    OperationQueryResponse,
)
from app.services.mapi_errors import MapiResponseError, MapiTransportError
from app.services.mapi_service import raise_for_hcp_status

logger = logging.getLogger(__name__)


class QueryService:
    """Low-level HTTP client for the HCP Metadata Query API."""

    def __init__(self, settings: MapiSettings):
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    # ── Client lifecycle ───────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                verify=self.settings.hcp_verify_ssl,
                timeout=self.settings.hcp_timeout,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Query execution ────────────────────────────────────────────────

    async def _post_query(
        self,
        tenant: str,
        body: dict[str, Any],
        *,
        username: str,
        password: str,
        auth_type: str | None = None,
    ) -> dict:
        url = query_url_for_tenant(tenant, self.settings.hcp_domain)
        if not url:
            raise MapiResponseError(
                "HCP domain not configured — cannot build query URL",
                http_status=400,
                hcp_status=0,
            )

        client = await self._get_client()
        headers = {
            "Authorization": get_hcp_auth_header(
                username, password, auth_type or self.settings.hcp_auth_type
            ),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            resp = await client.post(url, headers=headers, content=json.dumps(body))
        except httpx.TimeoutException:
            logger.error("Query API timeout: POST %s", url)
            raise MapiTransportError("HCP query timed out", http_status=504)
        except httpx.ConnectError:
            logger.error("Query API unreachable: POST %s", url)
            raise MapiTransportError("HCP query unreachable", http_status=502)
        except httpx.TransportError as exc:
            logger.error("Query API transport error: POST %s — %s", url, exc)
            raise MapiTransportError("HCP query connection error", http_status=502)

        raise_for_hcp_status(resp, "query")

        data = resp.json()
        # HCP wraps responses in {"queryResult": {...}} — unwrap it.
        if "queryResult" in data:
            data = data["queryResult"]
        return data

    async def object_query(
        self,
        tenant: str,
        query: ObjectQuery,
        *,
        username: str,
        password: str,
        auth_type: str | None = None,
    ) -> ObjectQueryResponse:
        request_body = ObjectQueryRequest(object=query)
        data = await self._post_query(
            tenant,
            request_body.model_dump(by_alias=True, exclude_none=True),
            username=username,
            password=password,
            auth_type=auth_type,
        )
        return ObjectQueryResponse.model_validate(data)

    async def operation_query(
        self,
        tenant: str,
        query: OperationQuery,
        *,
        username: str,
        password: str,
        auth_type: str | None = None,
    ) -> OperationQueryResponse:
        request_body = OperationQueryRequest(operation=query)
        data = await self._post_query(
            tenant,
            request_body.model_dump(by_alias=True, exclude_none=True),
            username=username,
            password=password,
            auth_type=auth_type,
        )
        return OperationQueryResponse.model_validate(data)


class AuthenticatedQueryService:
    """Wrapper that injects per-request credentials from the JWT.

    Uses composition: wraps any service that implements the same
    query interface (QueryService, CachedQueryService, etc.).
    """

    def __init__(
        self,
        base: QueryService,
        username: str,
        password: str,
    ):
        self._base = base
        self._username = username
        self._password = password

    @property
    def settings(self) -> MapiSettings:
        return self._base.settings

    async def object_query(
        self,
        tenant,
        query,
        *,
        username=None,
        password=None,
        auth_type=None,
    ):
        return await self._base.object_query(
            tenant,
            query,
            username=username or self._username,
            password=password or self._password,
            auth_type=auth_type,
        )

    async def operation_query(
        self,
        tenant,
        query,
        *,
        username=None,
        password=None,
        auth_type=None,
    ):
        return await self._base.operation_query(
            tenant,
            query,
            username=username or self._username,
            password=password or self._password,
            auth_type=auth_type,
        )

    async def close(self):
        pass  # base owns the client
