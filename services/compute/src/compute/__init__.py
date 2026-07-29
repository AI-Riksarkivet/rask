"""compute — Ray dashboard introspection (+ health) and the Ray Serve proxy.
Thin shell over ray-kit; no DB. The proxy_router mounts at the root (no /api/v1
prefix) so /api/serve/* reaches the Ray Serve status API.

The service is `compute` on every surface (R22, supersedes R20's `ray` choice):
uv member, import package, k8s objects, dapr app-id, image and gateway upstream
all align — and `import compute` shadows nothing, which kills R20's recorded
`ray-api`/`ray_api` PyPI-shadow exception. The PUBLIC URL namespace stays
`/api/ray` + `/api/serve`: those paths name the Ray cluster the endpoints
introspect, not this service."""

from compute import health, proxy, routes
from compute.lifespan import make_lifespan
from service_kit import make_service_app


app = make_service_app(
    title="compute",
    routers=[health.router, routes.router],
    proxy_router=proxy.router,
    lifespan=make_lifespan,
)
