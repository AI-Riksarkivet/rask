"""ray-api — Ray dashboard introspection (+ health) and the Ray Serve proxy.
Thin shell over ray-kit; no viewer, no DB. The proxy_router mounts at the root
(no /api/v1 prefix) so /api/serve/* reaches the Ray Serve status API."""

from ray_api import health, proxy, routes
from ray_api.lifespan import make_lifespan
from service_kit import make_service_app


app = make_service_app(
    title="ray-api",
    routers=[health.router, routes.router],
    proxy_router=proxy.router,
    lifespan=make_lifespan,
)
