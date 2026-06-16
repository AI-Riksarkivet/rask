"""orchestrator — the reconcile→derive→submit loop as its own process.

For the local trial this stays the in-process timer loop: `make_lifespan` starts
it when `RASK_ORCHESTRATOR_AUTOSTART=1` (set in `Procfile.micro`). Only `/health`
is exposed. The eventual production form is a NATS JetStream consumer.
"""

from service_kit import make_service_app
from viewer.api.v1.endpoints import health, orchestrator
from viewer.core.lifespan import make_lifespan


# Serves /api/orchestrator/{state,start,stop} so this process's own loop can be
# controlled at runtime (start/stop flip app.state.orchestrator_task here).
app = make_service_app(title="orchestrator", routers=[health.router, orchestrator.router], lifespan=make_lifespan)
