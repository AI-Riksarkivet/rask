"""core-api — the transitional core husk: EAD catalog search (+ health).

Post-P7a there is no batches table, no chunk submit, no orchestrator loop — ingestion is the
medallion IIIF producer and HTR runs as cascade compute on the lakehouse. This entrypoint retires
with the R6/R20 media wave (docs/architecture/lance-ns-merge.md P7d).
"""

from core.api.v1.endpoints import catalog, health
from core.lifespan import make_lifespan
from service_kit import make_service_app


app = make_service_app(
    title="core-api",
    routers=[health.router, catalog.router],
    lifespan=make_lifespan,
)
