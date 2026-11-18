"""Aggregate the always-on lineage v1 endpoint routers."""

from __future__ import annotations

from fastapi import APIRouter

from lineage.api.v1.endpoints import dlq, events, ingest, tables


# One rule, as the catalog's: each endpoint module's router carries its own prefix, and this list mounts them.
api_router = APIRouter()
for _router in (events.router, tables.router, dlq.router, ingest.router):
    api_router.include_router(_router)
