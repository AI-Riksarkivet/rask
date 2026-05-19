"""Shared client factory — auto-authenticates from config/flags/env."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from rahcp_client import HCPClient


def make_client(ctx: typer.Context) -> HCPClient:
    """Create an HCPClient from resolved settings.

    Priority: CLI flags > env vars > config file > defaults.

    If username/password are available, the client auto-logs in on __aenter__.
    Tenant is passed during login so the backend routes to the correct HCP tenant.
    """
    from rahcp_client import HCPClient

    return HCPClient(
        endpoint=ctx.obj["endpoint"],
        username=ctx.obj.get("username", ""),
        password=ctx.obj.get("password", ""),
        tenant=ctx.obj.get("tenant"),
        verify_ssl=ctx.obj.get("verify_ssl", True),
        timeout=ctx.obj.get("timeout", 30.0),
        multipart_threshold=ctx.obj.get("multipart_threshold", 100 * 1024 * 1024),
        multipart_chunk=ctx.obj.get("multipart_chunk", 64 * 1024 * 1024),
    )
