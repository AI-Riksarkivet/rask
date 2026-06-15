"""search-api — line-level FTS + thumbnails (+ health). LanceDB + S3, no DB."""

from service_kit import make_service_app
from viewer.api.v1.endpoints import health, search


app = make_service_app(title="search-api", routers=[health.router, search.router])
