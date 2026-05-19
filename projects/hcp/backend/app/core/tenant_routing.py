"""Tenant-aware host derivation for HCP virtual-hosted routing."""

from __future__ import annotations


def mapi_host_for_tenant(tenant: str | None, domain: str) -> str | None:
    """Derive the MAPI hostname for a tenant.

    Returns None when *domain* is not set (caller should fall back to
    the static ``hcp_host`` setting).
    """
    if not domain:
        return None
    return f"{tenant}.{domain}" if tenant else f"admin.{domain}"


def query_url_for_tenant(tenant: str | None, domain: str) -> str | None:
    """Derive the Metadata Query API URL for a tenant.

    Returns None when *domain* is not set or *tenant* is None (caller
    should skip the query).
    """
    if not domain or not tenant:
        return None
    return f"https://{tenant}.{domain}/query"


def s3_endpoint_for_tenant(tenant: str | None, domain: str) -> str | None:
    """Derive the S3 endpoint URL for a tenant.

    Returns None when *domain* is not set or *tenant* is None (caller
    should fall back to the static ``s3_endpoint_url`` setting).
    """
    if not domain or not tenant:
        return None
    return f"https://{tenant}.{domain}"
