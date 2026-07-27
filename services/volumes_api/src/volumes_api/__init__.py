"""volumes-api — image + ALTO serving over S3/IIIF (+ health). Stateless: builds
storage sources on demand from settings; no DB/Lance/Ray, no viewer dependency."""

from service_kit import make_service_app
from volumes_api import health, routes


app = make_service_app(title="volumes-api", routers=[health.router, routes.router])
