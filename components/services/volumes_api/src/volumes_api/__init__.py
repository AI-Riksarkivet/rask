"""volumes-api — image + ALTO serving over S3/IIIF (+ health). No DB."""

from service_kit import make_service_app
from viewer.api.v1.endpoints import health, volumes
from viewer.core.lifespan import make_lifespan


app = make_service_app(title="volumes-api", routers=[health.router, volumes.router], lifespan=make_lifespan)
