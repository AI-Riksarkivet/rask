"""ray — Ray dashboard introspection (+ health) and the Ray Serve proxy.
Thin shell over ray-kit; no DB. The proxy_router mounts at the root (no /api/v1
prefix) so /api/serve/* reaches the Ray Serve status API.

The k8s/dapr/image/gateway name is `ray` (R20 — the -api suffix died with the
R6/R20 wave); the uv member stays `ray-api` / import package `ray_api` because a
Python package named `ray` would shadow the PyPI `ray` that ray-kit depends on —
a recorded language-constraint exception."""

from ray_api import health, proxy, routes
from ray_api.lifespan import make_lifespan
from service_kit import make_service_app


app = make_service_app(
    title="ray",
    routers=[health.router, routes.router],
    proxy_router=proxy.router,
    lifespan=make_lifespan,
)
