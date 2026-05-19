"""Query test fixtures — provides MapiSettings with hcp_domain for QueryService."""

from __future__ import annotations

import pytest

from app.core.config import MapiSettings

QUERY_URL = "https://mock.hcp.example.com/query"


@pytest.fixture
def query_settings() -> MapiSettings:
    """MapiSettings with hcp_domain — required by QueryService."""
    return MapiSettings(
        hcp_host="mock.hcp.example.com",
        hcp_domain="hcp.example.com",
        hcp_port=9090,
        hcp_username="testuser",
        hcp_password="testpass",
        hcp_auth_type="hcp",
        hcp_verify_ssl=False,
        hcp_timeout=30,
    )
