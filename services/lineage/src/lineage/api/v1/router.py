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
for _module in (datasets, discovery, governance, columns, reconcile, runs, dlq):
    api_router.include_router(_module.router)
# The version prefix is a COMPOSITION decision, applied here rather than baked into the endpoint
# router (F-LIN-14: ingest was the one self-versioning router of eight). /api/v1/lineage is the
# OpenLineage HTTP-transport default path (OPENLINEAGE_URL + /api/v1/lineage) — an external
# contract every zero-glue producer relies on, so the mounted path must not move.
api_router.include_router(ingest.router, prefix="/api/v1")
