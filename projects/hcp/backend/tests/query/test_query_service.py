"""Unit tests for QueryService and AuthenticatedQueryService."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest

from app.core.config import MapiSettings
from app.schemas.query import ObjectQuery, OperationQuery
from app.services.mapi_errors import MapiResponseError, MapiTransportError
from app.services.query_service import AuthenticatedQueryService, QueryService

from .conftest import QUERY_URL


@pytest.fixture
async def svc(query_settings: MapiSettings) -> AsyncGenerator[QueryService, None]:
    s = QueryService(query_settings)
    yield s
    await s.close()


# ── Object query ──────────────────────────────────────────────────────


async def test_object_query_success(svc: QueryService, hcp_mock):
    hcp_mock.post(QUERY_URL).respond(
        200,
        json={
            "status": {"totalResults": 1, "results": 1, "code": "COMPLETE"},
            "resultSet": [
                {
                    "urlName": "/docs/file.pdf",
                    "operation": "CREATED",
                    "changeTimeMilliseconds": "1706745600000",
                    "version": "0",
                }
            ],
        },
    )

    result = await svc.object_query(
        "mock",
        ObjectQuery(query="*:*", count=5),
        username="testuser",
        password="testpass",
    )
    assert result.status.total_results == 1
    assert result.status.code == "COMPLETE"
    assert len(result.resultSet) == 1
    assert result.resultSet[0].url_name == "/docs/file.pdf"


async def test_object_query_empty_results(svc: QueryService, hcp_mock):
    hcp_mock.post(QUERY_URL).respond(
        200,
        json={
            "status": {"totalResults": 0, "results": 0, "code": "COMPLETE"},
            "resultSet": [],
        },
    )

    result = await svc.object_query(
        "mock",
        ObjectQuery(query="nonexistent"),
        username="testuser",
        password="testpass",
    )
    assert result.status.total_results == 0
    assert len(result.resultSet) == 0


async def test_object_query_verbose(svc: QueryService, hcp_mock):
    hcp_mock.post(QUERY_URL).respond(
        200,
        json={
            "status": {"totalResults": 1, "results": 1, "code": "COMPLETE"},
            "resultSet": [
                {
                    "urlName": "/docs/file.pdf",
                    "operation": "CREATED",
                    "changeTimeMilliseconds": "1706745600000",
                    "version": "0",
                    "size": 12345,
                    "contentType": "application/pdf",
                    "namespace": "documents",
                    "owner": "admin",
                }
            ],
        },
    )

    result = await svc.object_query(
        "mock",
        ObjectQuery(query="*:*", verbose=True, count=1),
        username="testuser",
        password="testpass",
    )
    assert result.resultSet[0].size == 12345
    assert result.resultSet[0].content_type == "application/pdf"


# ── Operation query ───────────────────────────────────────────────────


async def test_operation_query_success(svc: QueryService, hcp_mock):
    hcp_mock.post(QUERY_URL).respond(
        200,
        json={
            "status": {"totalResults": 2, "results": 2, "code": "COMPLETE"},
            "resultSet": [
                {
                    "urlName": "/docs/file.pdf",
                    "operation": "CREATED",
                    "changeTimeMilliseconds": "1706745600000",
                    "version": "0",
                },
                {
                    "urlName": "/docs/old.txt",
                    "operation": "DELETED",
                    "changeTimeMilliseconds": "1707350400000",
                    "version": "0",
                },
            ],
        },
    )

    result = await svc.operation_query(
        "mock", OperationQuery(count=10), username="testuser", password="testpass"
    )
    assert result.status.total_results == 2
    assert len(result.resultSet) == 2
    assert result.resultSet[1].operation == "DELETED"


# ── Error handling ────────────────────────────────────────────────────


async def test_query_hcp_error_raises(svc: QueryService, hcp_mock):
    hcp_mock.post(QUERY_URL).respond(403, text="Access denied")

    with pytest.raises(MapiResponseError) as exc_info:
        await svc.object_query(
            "mock", ObjectQuery(query="*:*"), username="testuser", password="testpass"
        )
    assert exc_info.value.http_status == 403


async def test_query_timeout_returns_504(svc: QueryService, hcp_mock):
    hcp_mock.post(QUERY_URL).mock(side_effect=httpx.TimeoutException("timeout"))

    with pytest.raises(MapiTransportError) as exc_info:
        await svc.object_query(
            "mock", ObjectQuery(query="*:*"), username="testuser", password="testpass"
        )
    assert exc_info.value.http_status == 504


async def test_query_connect_error_returns_502(svc: QueryService, hcp_mock):
    hcp_mock.post(QUERY_URL).mock(side_effect=httpx.ConnectError("unreachable"))

    with pytest.raises(MapiTransportError) as exc_info:
        await svc.object_query(
            "mock", ObjectQuery(query="*:*"), username="testuser", password="testpass"
        )
    assert exc_info.value.http_status == 502


async def test_query_no_domain_raises_400():
    """QueryService without hcp_domain should raise MapiResponseError."""
    settings = MapiSettings(
        hcp_host="mock.hcp.example.com",
        hcp_domain="",
        hcp_port=9090,
        hcp_username="testuser",
        hcp_password="testpass",
    )
    svc = QueryService(settings)
    with pytest.raises(MapiResponseError) as exc_info:
        await svc.object_query(
            "mock", ObjectQuery(query="*:*"), username="testuser", password="testpass"
        )
    assert exc_info.value.http_status == 400
    await svc.close()


# ── AuthenticatedQueryService ─────────────────────────────────────────


async def test_authenticated_passes_credentials(svc: QueryService, hcp_mock):
    route = hcp_mock.post(QUERY_URL).respond(
        200,
        json={
            "status": {"totalResults": 0, "results": 0, "code": "COMPLETE"},
            "resultSet": [],
        },
    )

    auth_svc = AuthenticatedQueryService(svc, "myuser", "mypass")
    await auth_svc.object_query("mock", ObjectQuery(query="*:*"))

    assert route.called
    request = route.calls[0].request
    assert "Authorization" in request.headers
    # HCP auth: base64(user):md5(pass)
    assert request.headers["Authorization"].startswith("HCP ")


async def test_authenticated_close_is_noop(svc: QueryService):
    auth_svc = AuthenticatedQueryService(svc, "u", "p")
    await auth_svc.close()  # Should not raise
