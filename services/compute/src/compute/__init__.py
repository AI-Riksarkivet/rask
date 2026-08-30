"""compute — Ray dashboard introspection (+ health) and the Ray Serve proxy.
Thin shell over ray-kit; no DB. The proxy_router mounts at the root (no /api/v1
prefix) so /api/serve/* reaches the Ray Serve status API.

The service is `compute` on every surface (R22, supersedes R20's `ray` choice):
uv member, import package, k8s objects, dapr app-id, image and gateway upstream
all align — and `import compute` shadows nothing, which kills R20's recorded
`ray-api`/`ray_api` PyPI-shadow exception. The PUBLIC URL namespace stays
`/api/ray` + `/api/serve`: those paths name the Ray cluster the endpoints
introspect, not this service."""

from fastapi import APIRouter

from compute import health, proxy, pruner, routes
from compute.dependencies import get_compute_settings
from compute.lifespan import make_lifespan
from service_kit import make_service_app


# ONE root-mounted router composed from the two root concerns: the Serve proxy and the Dapr cron
# binding (a binding is delivered to POST /<name> at the ROOT, never under the api prefix). Composed
# here so proxy.py and pruner.py stay ignorant of each other. The cron router is BUILT from the
# settings because its path is the binding's name — the one piece of this service's configuration
# that genuinely has to be known at app-construction time.
_root = APIRouter()
_root.include_router(proxy.router)
_root.include_router(pruner.make_pruner_router(get_compute_settings()))

app = make_service_app(
    title="compute",
    routers=[health.router, routes.router],
    proxy_router=_root,
    lifespan=make_lifespan,
)
