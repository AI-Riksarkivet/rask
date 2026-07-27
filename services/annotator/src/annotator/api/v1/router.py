"""Router aggregation for the annotator service (lance-ns ``api/v1/router.py``).

The annotations resource keeps its package layout (its own router composition);
assist + jobs are single-module endpoints. Wire paths stay ``/api/*``.
"""

from fastapi import APIRouter

from annotator.annotations.router import router as annotations_router
from annotator.api.v1.endpoints.assist import router as assist_router
from annotator.api.v1.endpoints.jobs import router as jobs_router


router = APIRouter()
for r in (annotations_router, assist_router, jobs_router):
    router.include_router(r)
