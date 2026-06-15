"""core-api — the state-mutating core: batches, chunks, catalog (+ health).

Owns the Postgres `batches` table and Ray submit for chunk ops. Runs WITHOUT the
orchestrator loop (that is its own process); ensure `RASK_ORCHESTRATOR_AUTOSTART`
is unset/0 for this service.
"""

from service_kit import make_service_app
from viewer.api.v1.endpoints import batches, catalog, chunks, health
from viewer.core.lifespan import make_lifespan


app = make_service_app(
    title="core-api",
    routers=[health.router, batches.router, chunks.router, catalog.router],
    lifespan=make_lifespan,
)
