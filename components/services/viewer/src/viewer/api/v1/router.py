"""Aggregator router for v1.

Each endpoint module declares only its own resource prefix (`/batches`,
`/search`, …); this aggregator adds none, and `main.py` applies the v1 prefix
(`settings.api_prefix`, default `/api/v1`) once at the include site. So any
server-generated URL must be built from `settings.api_prefix`, never a
hardcoded `/api/...` literal."""

from fastapi import APIRouter

from viewer.api.v1.endpoints import batches, catalog, chunks, health, orchestrator, ray, search


api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(batches.router)
api_router.include_router(chunks.router)
api_router.include_router(search.router)
api_router.include_router(catalog.router)
api_router.include_router(orchestrator.router)
api_router.include_router(ray.router)
