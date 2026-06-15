"""volumes-api — image + ALTO serving over S3/IIIF (+ health). No DB."""

from backends._common import make_service_app
from viewer.api.v1.endpoints import health, volumes


app = make_service_app(title="volumes-api", routers=[health.router, volumes.router])
