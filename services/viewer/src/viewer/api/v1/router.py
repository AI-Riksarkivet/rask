"""Router aggregation for the viewer service (lance-ns ``api/v1/router.py``).

Wire paths stay ``/api/*`` (the shipped frontend contract); the v1 directory is
the structural convention, not the URL.
"""

from fastapi import APIRouter

from viewer.api.v1.endpoints.atlas import router as atlas_router
from viewer.api.v1.endpoints.datasets import router as datasets_router
from viewer.api.v1.endpoints.diarization import router as diarization_router
from viewer.api.v1.endpoints.graph import router as graph_router
from viewer.api.v1.endpoints.media import router as media_router
from viewer.api.v1.endpoints.objects import router as objects_router
from viewer.api.v1.endpoints.pages import router as pages_router
from viewer.api.v1.endpoints.system import router as system_router
from viewer.api.v1.endpoints.topics import router as topics_router
from viewer.api.v1.endpoints.transcripts import router as transcripts_router
from viewer.api.v1.endpoints.voice import router as voice_router


router = APIRouter()
for r in (
    datasets_router,
    media_router,
    objects_router,
    pages_router,
    transcripts_router,
    system_router,
    atlas_router,
    voice_router,
    diarization_router,
    topics_router,
    graph_router,
):
    router.include_router(r)
