"""Aggregator router for v1.

Endpoint modules own their full `/api/...` paths (no prefix added here) so the
include site in `main.py` doesn't have to special-case paths that live outside
`/api/` (e.g. the SPA fallback)."""

from fastapi import APIRouter

from viewer.api.v1.endpoints import batches, catalog, chunks, health, orchestrator, ray, search, volumes


api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(volumes.router)
api_router.include_router(batches.router)
api_router.include_router(chunks.router)
api_router.include_router(search.router)
api_router.include_router(catalog.router)
api_router.include_router(orchestrator.router)
api_router.include_router(ray.router)
