"""search-api — line-level FTS + thumbnails (+ health). LanceDB + S3, no DB."""

from service_kit import make_service_app
from viewer.api.v1.endpoints import health, search
from viewer.core.lifespan import make_lifespan


app = make_service_app(title="search-api", routers=[health.router, search.router], lifespan=make_lifespan)
