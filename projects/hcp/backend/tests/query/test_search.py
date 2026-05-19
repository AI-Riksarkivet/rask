"""Integration tests for the Metadata Query API endpoints."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import (
    get_mapi_service,
    get_mapi_settings,
    get_query_service,
    get_s3_service,
)
from app.core.config import AuthSettings, MapiSettings
from app.main import app
from app.services.mapi_service import AuthenticatedMapiService, MapiService
from app.services.query_service import AuthenticatedQueryService, QueryService

from .conftest import QUERY_URL


@pytest.fixture
async def client(
    hcp_mock, query_settings: MapiSettings, auth_settings: AuthSettings
) -> AsyncGenerator[AsyncClient, None]:
    """Async test client with a real QueryService backed by respx."""
    mapi_svc = MapiService(query_settings)
    query_svc = QueryService(query_settings)
    auth_mapi = AuthenticatedMapiService(mapi_svc, "testuser", "testpass")
    auth_query = AuthenticatedQueryService(query_svc, "testuser", "testpass")

    mock_s3 = None  # Not needed for query tests

    async def _override_s3():
        yield mock_s3

    async def _override_mapi():
        yield auth_mapi

    async def _override_query():
        yield auth_query

    def _override_mapi_settings():
        return query_settings

    app.dependency_overrides[get_s3_service] = _override_s3
    app.dependency_overrides[get_mapi_service] = _override_mapi
    app.dependency_overrides[get_query_service] = _override_query
    app.dependency_overrides[get_mapi_settings] = _override_mapi_settings

    app.state.cache = None
    app.state.mapi = mapi_svc
    app.state.query = query_svc
    app.state.s3_cache = {}

    with patch("app.core.security._get_auth_settings", return_value=auth_settings):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    await query_svc.close()
    await mapi_svc.close()
    app.dependency_overrides.clear()


# ── Object query endpoint ─────────────────────────────────────────────


async def test_query_objects_success(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
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

    resp = await client.post(
        "/api/v1/query/tenants/mock/objects",
        headers=auth_headers,
        json={"query": "*:*", "count": 5},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"]["totalResults"] == 1
    assert data["status"]["code"] == "COMPLETE"
    assert len(data["resultSet"]) == 1
    assert data["resultSet"][0]["urlName"] == "/docs/file.pdf"


async def test_query_objects_verbose(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
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
                    "size": 2458624,
                    "contentType": "application/pdf",
                    "namespace": "documents",
                }
            ],
        },
    )

    resp = await client.post(
        "/api/v1/query/tenants/mock/objects",
        headers=auth_headers,
        json={"query": "*:*", "verbose": True, "count": 1},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["resultSet"][0]["size"] == 2458624
    assert data["resultSet"][0]["contentType"] == "application/pdf"


async def test_query_objects_empty(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(QUERY_URL).respond(
        200,
        json={
            "status": {"totalResults": 0, "results": 0, "code": "COMPLETE"},
            "resultSet": [],
        },
    )

    resp = await client.post(
        "/api/v1/query/tenants/mock/objects",
        headers=auth_headers,
        json={"query": "nonexistent"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"]["totalResults"] == 0
    assert data["resultSet"] == []


async def test_query_objects_with_namespace_filter(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(QUERY_URL).respond(
        200,
        json={
            "status": {"totalResults": 3, "results": 3, "code": "COMPLETE"},
            "resultSet": [
                {"urlName": "/docs/a.pdf", "operation": "CREATED", "version": "0"},
                {"urlName": "/docs/b.pdf", "operation": "CREATED", "version": "0"},
                {"urlName": "/docs/c.pdf", "operation": "CREATED", "version": "0"},
            ],
        },
    )

    resp = await client.post(
        "/api/v1/query/tenants/mock/objects",
        headers=auth_headers,
        json={"query": 'namespace:"documents"', "count": 10},
    )
    assert resp.status_code == 200
    assert route.called


# ── Operation query endpoint ──────────────────────────────────────────


async def test_query_operations_success(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
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
                    "changeTimeMilliseconds": "1710979200000",
                    "version": "0",
                },
            ],
        },
    )

    resp = await client.post(
        "/api/v1/query/tenants/mock/operations",
        headers=auth_headers,
        json={"count": 10},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"]["totalResults"] == 2
    assert len(data["resultSet"]) == 2
    assert data["resultSet"][1]["operation"] == "DELETED"


async def test_query_operations_with_filters(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(QUERY_URL).respond(
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

    resp = await client.post(
        "/api/v1/query/tenants/mock/operations",
        headers=auth_headers,
        json={
            "count": 10,
            "systemMetadata": {
                "transactions": {"transaction": ["create"]},
                "namespaces": {"namespace": ["documents"]},
            },
        },
    )
    assert resp.status_code == 200
    assert route.called


# ── Auth required ─────────────────────────────────────────────────────


async def test_query_objects_requires_auth(client: AsyncClient):
    resp = await client.post(
        "/api/v1/query/tenants/mock/objects",
        json={"query": "*:*"},
    )
    assert resp.status_code == 401


async def test_query_operations_requires_auth(client: AsyncClient):
    resp = await client.post(
        "/api/v1/query/tenants/mock/operations",
        json={"count": 10},
    )
    assert resp.status_code == 401


# ── HCP errors ────────────────────────────────────────────────────────


async def test_query_objects_hcp_error(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(QUERY_URL).respond(403, text="Access denied")

    resp = await client.post(
        "/api/v1/query/tenants/mock/objects",
        headers=auth_headers,
        json={"query": "*:*"},
    )
    assert resp.status_code == 403
