"""HCPClient — async HTTP client with auth, retry, tracing, and auto-refresh."""

from __future__ import annotations

import logging
import time
from types import TracebackType
from typing import TYPE_CHECKING, Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from rahcp_client.config import HCPSettings
from rahcp_client.errors import (
    AuthenticationError,
    RetryableError,
    error_for_status,
)
from rahcp_client.tracing import tracer

if TYPE_CHECKING:
    from rahcp_client.bulk.protocol import TransferSettings
    from rahcp_client.mapi import MapiOps
    from rahcp_client.s3 import S3Ops

log = logging.getLogger(__name__)

_RETRYABLE_STATUSES = frozenset({408, 429, 500, 503, 504})
_SENSITIVE_KEYS = {"password", "token", "access_token", "secret", "authorization"}


def _redact(text: str, max_len: int = 200) -> str:
    """Truncate and redact sensitive values from error/log messages."""
    import json as _json

    truncated = text[:max_len]
    try:
        parsed = _json.loads(truncated)
        if isinstance(parsed, dict):
            for key in parsed:
                if key.lower() in _SENSITIVE_KEYS:
                    parsed[key] = "[REDACTED]"
            return _json.dumps(parsed)
    except (ValueError, TypeError):
        pass
    return truncated


class HCPClient:
    """Async HTTP client for the HCP Unified API.

    Usage::

        async with HCPClient.from_env() as hcp:
            buckets = await hcp.s3.list_objects("bucket", "prefix/")
            ns = await hcp.mapi.list_namespaces("tenant")
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:8000/api/v1",
        username: str = "",
        password: str = "",
        tenant: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 4,
        retry_base_delay: float = 1.0,
        multipart_threshold: int = 100 * 1024 * 1024,
        multipart_chunk: int = 64 * 1024 * 1024,
        multipart_concurrency: int = 6,
        verify_ssl: bool = True,
    ) -> None:
        """Initialize the HCP client.

        Args:
            endpoint: Base URL for the HCP Unified API.
            username: Username for authentication.
            password: Password for authentication.
            tenant: HCP tenant name (routes requests to the correct tenant).
            timeout: HTTP request timeout in seconds.
            max_retries: Maximum number of retry attempts for transient errors.
            retry_base_delay: Base delay between retries in seconds (doubles each attempt).
            multipart_threshold: File size in bytes above which multipart upload is used.
            multipart_chunk: Part size in bytes for multipart uploads.
            verify_ssl: Whether to verify TLS certificates.
        """
        self.endpoint = endpoint.rstrip("/")
        self.username = username
        self.password = password
        self.tenant = tenant
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.multipart_threshold = multipart_threshold
        self.multipart_chunk = multipart_chunk
        self.multipart_concurrency = multipart_concurrency
        self.verify_ssl = verify_ssl

        self._http = httpx.AsyncClient(
            base_url=self.endpoint,
            timeout=timeout,
            verify=verify_ssl,
        )
        self._token: str | None = None
        self._s3: S3Ops | None = None
        self._mapi: MapiOps | None = None

    def __repr__(self) -> str:
        return (
            f"HCPClient(endpoint={self.endpoint!r}, user={self.username!r}, "
            f"tenant={self.tenant!r})"
        )

    @property
    def token(self) -> str | None:
        """The current bearer token (set after login)."""
        return self._token

    @classmethod
    def from_env(cls) -> HCPClient:
        """Create a client configured from ``HCP_*`` environment variables."""
        settings = HCPSettings()
        return cls(
            endpoint=settings.endpoint,
            username=settings.username,
            password=settings.password,
            tenant=settings.tenant,
            timeout=settings.timeout,
            max_retries=settings.max_retries,
            retry_base_delay=settings.retry_base_delay,
            multipart_threshold=settings.multipart_threshold,
            multipart_chunk=settings.multipart_chunk,
            multipart_concurrency=settings.multipart_concurrency,
            verify_ssl=settings.verify_ssl,
        )

    async def __aenter__(self) -> HCPClient:
        try:
            if self.username and self.password:
                await self._login()
        except Exception:
            await self._http.aclose()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    @property
    def s3(self) -> S3Ops:
        """S3 data-plane operations."""
        if self._s3 is None:
            from rahcp_client.s3 import S3Ops

            self._s3 = S3Ops(self)
        return self._s3

    @property
    def mapi(self) -> MapiOps:
        """MAPI management-plane operations."""
        if self._mapi is None:
            from rahcp_client.mapi import MapiOps

            self._mapi = MapiOps(self)
        return self._mapi

    @property
    def transfer_settings(self) -> TransferSettings:
        """Settings needed by the bulk transfer engine."""
        from rahcp_client.bulk.protocol import TransferSettings

        return TransferSettings(
            verify_ssl=self.verify_ssl,
            timeout=self.timeout,
            multipart_threshold=self.multipart_threshold,
        )

    async def _login(self) -> None:
        """Authenticate and store the bearer token."""
        form: dict[str, str] = {"username": self.username, "password": self.password}
        if self.tenant:
            form["tenant"] = self.tenant
        response = await self._http.post("/auth/token", data=form)
        if response.status_code != 200:
            raise AuthenticationError(
                f"Login failed: {response.status_code}",
                status_code=response.status_code,
            )
        data = response.json()
        self._token = data["access_token"]
        log.info(
            "Authenticated as %s (tenant=%s)", self.username, self.tenant or "system"
        )

    def _auth_headers(self) -> dict[str, str]:
        """Return authorization headers if a token is available."""
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send an HTTP request with retry, tracing, and automatic 401 refresh."""
        merged_headers = {**self._auth_headers(), **(headers or {})}
        refreshed = False

        with tracer.start_as_current_span(f"{method} {path}") as span:
            span.set_attribute("http.method", method)
            span.set_attribute("http.path", path)

            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.max_retries + 1),
                wait=wait_exponential_jitter(
                    initial=self.retry_base_delay,
                    max=60,
                    jitter=self.retry_base_delay,
                ),
                retry=retry_if_exception_type(RetryableError),
                reraise=True,
            ):
                with attempt:
                    t0 = time.monotonic()
                    try:
                        response = await self._http.request(
                            method,
                            path,
                            params=params,
                            json=json,
                            data=data,
                            content=content,
                            headers=merged_headers,
                        )
                    except httpx.TransportError as exc:
                        duration = (time.monotonic() - t0) * 1000
                        log.warning(
                            "%s %s — transport error after %.0fms: %s",
                            method,
                            path,
                            duration,
                            exc,
                        )
                        raise RetryableError(str(exc)) from exc

                    duration = (time.monotonic() - t0) * 1000
                    span.set_attribute("http.status_code", response.status_code)

                    log.debug(
                        "%s %s → %d (%.0fms)",
                        method,
                        path,
                        response.status_code,
                        duration,
                    )

                    # One-time token refresh on 401
                    if response.status_code == 401 and self.username and not refreshed:
                        log.info("Token expired, refreshing...")
                        await self._login()
                        merged_headers = {**self._auth_headers(), **(headers or {})}
                        refreshed = True
                        raise RetryableError("token refresh")

                    if response.status_code in _RETRYABLE_STATUSES:
                        raise error_for_status(
                            response.status_code, _redact(response.text)
                        )

                    if response.status_code >= 400:
                        safe_body = _redact(response.text)
                        log_fn = log.debug if response.status_code == 404 else log.error
                        log_fn(
                            "%s %s → %d (%.0fms): %s",
                            method,
                            path,
                            response.status_code,
                            duration,
                            safe_body,
                        )
                        raise error_for_status(response.status_code, safe_body)

                    return response

        raise RetryableError("All retries exhausted")  # pragma: no cover
