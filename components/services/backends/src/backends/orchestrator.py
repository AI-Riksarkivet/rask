"""orchestrator — the reconcile→derive→submit loop as its own process.

For the local trial this stays the in-process timer loop: `make_lifespan` starts
it when `RASK_ORCHESTRATOR_AUTOSTART=1` (set in `Procfile.micro`). Only `/health`
is exposed. The eventual production form is a NATS JetStream consumer.
"""

from backends._common import make_service_app
from viewer.api.v1.endpoints import health


app = make_service_app(title="orchestrator", routers=[health.router])
