"""Tests for app.core.tenant_routing host derivation functions."""

from __future__ import annotations

from app.core.tenant_routing import (
    mapi_host_for_tenant,
    query_url_for_tenant,
    s3_endpoint_for_tenant,
)


# ── mapi_host_for_tenant ─────────────────────────────────────────────


def test_mapi_host_empty_domain_returns_none():
    assert mapi_host_for_tenant("dev-ai", "") is None


def test_mapi_host_system_admin():
    assert mapi_host_for_tenant(None, "hcp.example.com") == "admin.hcp.example.com"


def test_mapi_host_tenant_admin():
    assert mapi_host_for_tenant("dev-ai", "hcp.example.com") == "dev-ai.hcp.example.com"


# ── s3_endpoint_for_tenant ───────────────────────────────────────────


def test_s3_endpoint_empty_domain_returns_none():
    assert s3_endpoint_for_tenant("dev-ai", "") is None


def test_s3_endpoint_no_tenant_returns_none():
    assert s3_endpoint_for_tenant(None, "hcp.example.com") is None


def test_s3_endpoint_tenant():
    assert (
        s3_endpoint_for_tenant("dev-ai", "hcp.example.com")
        == "https://dev-ai.hcp.example.com"
    )


# ── query_url_for_tenant ─────────────────────────────────────────────


def test_query_url_empty_domain_returns_none():
    assert query_url_for_tenant("dev-ai", "") is None


def test_query_url_no_tenant_returns_none():
    assert query_url_for_tenant(None, "hcp.example.com") is None


def test_query_url_tenant():
    assert (
        query_url_for_tenant("dev-ai", "hcp.example.com")
        == "https://dev-ai.hcp.example.com/query"
    )
