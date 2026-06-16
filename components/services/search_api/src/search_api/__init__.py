"""search-api — line-level FTS + thumbnails (+ health). LanceDB + S3; no viewer, no DB/Ray."""

from search_api import health, routes
from search_api.lifespan import make_lifespan
from service_kit import make_service_app


app = make_service_app(title="search-api", routers=[health.router, routes.router], lifespan=make_lifespan)
