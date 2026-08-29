"""Path versioning is a composition decision, not an endpoint router's (F-LIN-14).

Seven of the eight routers under ``api/v1/endpoints/`` are unversioned — the composition
layer (``api/v1/router.py``) decides where they mount. ``ingest`` alone baked ``/api/v1``
into its own ``APIRouter``, so the version prefix lived in two different layers depending
on which file you read. The prefix is hoisted to the composition point; the PUBLIC path
``/api/v1/lineage`` must not move — it is the OpenLineage HTTP-transport default
(``OPENLINEAGE_URL`` + ``/api/v1/lineage``), the zero-glue contract every external
producer (lineage-kit, Airflow, Spark, dbt) relies on.
"""

from __future__ import annotations

from fastapi import FastAPI
from lineage.api.v1.endpoints import (
    columns,
    datasets,
    demo,
    discovery,
    dlq,
    governance,
    ingest,
    reconcile,
    runs,
)
from lineage.api.v1.router import api_router


def test_no_endpoint_router_carries_a_version_prefix() -> None:
    """No router under ``api/v1/endpoints/`` self-versions — mounting is the composition layer's call."""
    for module in (columns, datasets, demo, discovery, dlq, governance, ingest, reconcile, runs):
        prefix = module.router.prefix
        assert not prefix.startswith("/api"), f"{module.__name__} bakes a version prefix into its own router: {prefix!r}"


def test_the_openlineage_transport_path_is_still_served() -> None:
    """The hoist must not move the OpenLineage HTTP-transport contract path."""
    app = FastAPI()
    app.include_router(api_router)
    # The openapi schema, not app.routes: FastAPI defers included routers (_IncludedRouter),
    # so the finalized path table is what proves where the route actually mounts.
    ingest_paths = [path for path, ops in app.openapi()["paths"].items() if "post" in ops and path.endswith("/lineage")]
    assert ingest_paths == ["/api/v1/lineage"]
