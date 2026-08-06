"""flows — the studio flow-builder's server half.

Three seams and no cluster of its own: a server-DECLARED node catalog (so a server-side node kind can
appear in the palette without a frontend release), PURE graph validation shared with the frontend
executor's vocabulary, and run execution — inline in v0, and over Dapr Workflow whenever a sidecar is
present, which is what makes an in-cluster run durable.

Fleet layout (`make_service_app` + flat modules + an injectable lifespan), like `compute`. The public
namespace is `/api/flows`; the gateway forwards that row unrewritten because this app mounts its
routers under `RASK_API_PREFIX` (`/api` in the chart and in `dev-micro.sh`) itself.
"""

from flows import health, routes
from flows.lifespan import make_lifespan
from service_kit import make_service_app


app = make_service_app(
    title="flows",
    routers=[health.router, routes.router],
    lifespan=make_lifespan,
)
