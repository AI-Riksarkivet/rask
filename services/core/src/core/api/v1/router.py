"""Aggregator router for v1.

Each endpoint module declares only its own resource prefix (`/catalog`, …); this aggregator adds
none, and `main.py` applies the v1 prefix (`settings.api_prefix`, default `/api/v1`) once at the
include site. Post-P7a husk: health + the EAD catalog search only — batches/chunks/orchestrator died
with the compute-plane cutover (docs/architecture/lance-ns-merge.md P7)."""

from fastapi import APIRouter

from core.api.v1.endpoints import catalog, health


api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(catalog.router)
