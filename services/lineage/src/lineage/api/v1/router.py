"""Aggregate the always-on lineage v1 endpoint routers."""

from __future__ import annotations

from fastapi import APIRouter

from lineage.api.v1.endpoints import (
    columns,
    datasets,
    discovery,
    dlq,
    governance,
    ingest,
    reconcile,
    runs,
)


api_router = APIRouter()
for _module in (datasets, discovery, governance, columns, reconcile, runs, ingest, dlq):
    api_router.include_router(_module.router)
