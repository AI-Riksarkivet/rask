"""HCP Management API HTTP client.

Handles authentication (HCP token + AD), request building,
and communication with the HCP system.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from app.services.cached_mapi import CachedMapiService
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel

from app.core.auth_utils import get_hcp_auth_header
from app.core.config import MapiSettings
from app.services.mapi_errors import MapiResponseError, MapiTransportError

logger = logging.getLogger(__name__)

# ── HCP status code → (http_status, message template) ─────────────────
_HCP_STATUS_MAP: Final[dict[int, tuple[int, str]]] = {
    302: (404, "{resource} not found or no permission"),
    400: (400, "{detail}"),
    401: (401, "HCP authentication failed"),
    403: (403, "{detail}"),
    404: (404, "{resource} not found"),
    405: (405, "Method not allowed for this resource"),
    409: (409, "{resource}: {detail}"),
    414: (414, "Request URI too large"),
    415: (415, "Unsupported media type"),
    500: (502, "HCP internal error: {detail}"),
    503: (503, "HCP unavailable: {detail}"),
}


def raise_for_hcp_status(resp: httpx.Response, resource: str = "resource") -> None:
    """Raise ``MapiResponseError`` for non-2xx HCP responses."""
    code = resp.status_code
    if 200 <= code < 300:
        return

    detail = resp.headers.get("X-HCP-ErrorMessage", resp.text or f"HCP returned {code}")
    entry = _HCP_STATUS_MAP.get(code)
    if entry:
        http_status, template = entry
        msg = template.format(resource=resource, detail=detail)
    else:
        http_status = 502
        msg = f"Unexpected HCP status {code}: {detail}"

    raise MapiResponseError(msg, http_status=http_status, hcp_status=code)


def parse_json_response(resp: httpx.Response) -> dict:
    """Parse JSON response body, returning empty dict on empty body."""
    if 200 <= resp.status_code < 300 and resp.content:
        try:
            return resp.json()
        except (ValueError, TypeError):
            return {}
    return {}


class MapiService:
    """Low-level HTTP client for the HCP management API."""

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

    # ── URL building ───────────────────────────────────────────────────

    def _build_url(
        self,
        path: str,
        host: str | None = None,
        query: dict[str, Any] | None = None,
    ) -> str:
        """Build full URL for a MAPI request."""
        h = host or self.settings.hcp_host
        port = self.settings.hcp_port
        url = f"https://{h}:{port}/mapi{path}"
        if query:
            filtered = {k: v for k, v in query.items() if v is not None}
            if filtered:
                url += "?" + urlencode(filtered, doseq=True)
        return url

    # ── Generic request ────────────────────────────────────────────────

    async def request(
        self,
        method: str,
        path: str,
        *,
        host: str | None = None,
        body: Any | None = None,
        query: dict[str, Any] | None = None,
        content_type: str = "application/json",
        accept: str = "application/json",
        username: str,
        password: str,
        auth_type: str | None = None,
        raw_body: bytes | None = None,
    ) -> httpx.Response:
        """Execute an HTTP request against the HCP management API."""
        client = await self._get_client()
        url = self._build_url(path, host=host, query=query)

        headers = {
            "Authorization": get_hcp_auth_header(
                username, password, auth_type or self.settings.hcp_auth_type
            ),
            "Accept": accept,
        }

        content = None
        if raw_body is not None:
            content = raw_body
            headers["Content-Type"] = content_type
        elif body is not None:
            if isinstance(body, BaseModel):
                content = body.model_dump_json(exclude_none=True).encode()
            elif isinstance(body, dict):
                content = json.dumps(body).encode()
            elif isinstance(body, str):
                content = body.encode()
            else:
                content = body
            headers["Content-Type"] = content_type

        try:
            resp = await client.request(method, url, headers=headers, content=content)
        except httpx.TimeoutException:
            logger.error("MAPI timeout: %s %s", method, path)
            raise MapiTransportError("HCP timed out", http_status=504)
        except httpx.ConnectError:
            logger.error("MAPI unreachable: %s %s", method, path)
            raise MapiTransportError("HCP unreachable", http_status=502)
        except httpx.TransportError as exc:
            logger.error("MAPI transport error: %s %s — %s", method, path, exc)
            raise MapiTransportError("HCP connection error", http_status=502)

        if resp.status_code >= 400:
            logger.warning("MAPI %s %s → %s", method, path, resp.status_code)

        return resp

    # ── Connectivity check ─────────────────────────────────────────────

    async def ping(self) -> bool:
        """Return True if HCP is reachable, False otherwise.

        Sends an unauthenticated HEAD — any response (including 401/403)
        proves the network path is up.  Only transport errors mean
        "unreachable".
        """
        try:
            client = await self._get_client()
            url = self._build_url("/tenants")
            await client.head(url, timeout=5.0)
            return True
        except Exception:
            return False

    # ── Convenience methods ────────────────────────────────────────────

    async def get(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("GET", path, **kwargs)

    async def put(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("PUT", path, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("POST", path, **kwargs)

    async def delete(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("DELETE", path, **kwargs)

    # ── High-level helpers (raise on error) ───────────────────────────

    async def fetch_json(
        self, path: str, *, resource: str = "resource", **kwargs
    ) -> dict:
        """GET + raise_for_hcp_status + parse JSON. One-liner for read endpoints."""
        resp = await self.get(path, **kwargs)
        raise_for_hcp_status(resp, resource)
        return parse_json_response(resp)

    async def send(
        self, method: str, path: str, *, resource: str = "resource", **kwargs
    ) -> httpx.Response:
        """request + raise_for_hcp_status. Returns the validated response."""
        resp = await self.request(method, path, **kwargs)
        raise_for_hcp_status(resp, resource)
        return resp


class AuthenticatedMapiService:
    """Wrapper that injects per-request credentials from the JWT.

    Uses composition: wraps any service that implements the same
    ``request()`` interface (MapiService, CachedMapiService, etc.).
    """

    def __init__(
        self,
        base: MapiService | CachedMapiService,
        username: str,
        password: str,
        host: str | None = None,
    ):
        self._base = base
        self._username = username
        self._password = password
        self._host = host

    @property
    def settings(self) -> MapiSettings:
        return self._base.settings

    async def request(
        self, method, path, *, username=None, password=None, host=None, **kwargs
    ):
        return await self._base.request(
            method,
            path,
            username=username or self._username,
            password=password or self._password,
            host=host or self._host,
            **kwargs,
        )

    async def get(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("GET", path, **kwargs)

    async def put(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("PUT", path, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("POST", path, **kwargs)

    async def delete(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("DELETE", path, **kwargs)

    async def fetch_json(
        self, path: str, *, resource: str = "resource", **kwargs
    ) -> dict:
        """GET + raise_for_hcp_status + parse JSON, with injected credentials."""
        resp = await self.get(path, **kwargs)
        raise_for_hcp_status(resp, resource)
        return parse_json_response(resp)

    async def send(
        self, method: str, path: str, *, resource: str = "resource", **kwargs
    ) -> httpx.Response:
        """request + raise_for_hcp_status, with injected credentials."""
        resp = await self.request(method, path, **kwargs)
        raise_for_hcp_status(resp, resource)
        return resp

    async def ping(self) -> bool:
        return await self._base.ping()

    async def close(self):
        pass  # base owns the client
