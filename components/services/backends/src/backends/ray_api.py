"""ray-api — Ray dashboard introspection (+ health) and the Ray Serve proxy.

The `proxy_router` is mounted at the root (no `/api/v1` prefix), exactly as in
`viewer.main`, so `/api/serve/*` reaches the Ray Serve status API.
"""

from backends._common import make_service_app
from viewer.api.v1.endpoints import health, ray


app = make_service_app(
    title="ray-api",
    routers=[health.router, ray.router],
    proxy_router=ray.proxy_router,
)
