"""Aggregate all v1 endpoint routers."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from catalog.api.fga_deps import authorize, require_request_names
from catalog.api.security import authenticate
from catalog.api.v1.endpoints import (
    access,
    access_admin,
    branches,
    columns,
    credentials,
    data,
    decisions,
    erasure,
    events,
    gates,
    indices,
    maintenance,
    me,
    members,
    models,
    namespaces,
    policies,
    projects,
    publication,
    stores,
    tables,
    tags,
    transactions,
    user_state,
    versions,
    views,
    warehouses,
)


# Every route passes three rungs before its door runs, in the order every door judges a request: who is asking (401,
# `authenticate`), whether the request names only what a door can read (400, `require_request_names`), and whether the
# caller may make it (403, `authorize`; a no-op when authentication and authorization are both off). Router-level, so
# the next route added inherits all three.
api_router = APIRouter(dependencies=[Depends(authenticate), Depends(require_request_names), Depends(authorize)])
for _module in (
    erasure,
    namespaces,
    tables,
    data,
    columns,
    indices,
    tags,
    branches,
    versions,
    transactions,
    views,
    credentials,
    warehouses,
    models,
    policies,
    access,
    access_admin,
    decisions,
    events,
    projects,
    me,
    maintenance,
    members,
    publication,
    user_state,
    stores,
    gates,
):
    api_router.include_router(_module.router)
    # A module may ALSO carry a `management_router`, mounted at `/management/v1/...`. That is the seam
    # [[LH-021]] moves rask-only operations across: a spec client meeting a rask verb on a spec prefix
    # has no way to tell the two apart, so the estate's own governance gets its own mount the way
    # Lakekeeper separates `/management` from `/catalog`. Both routers inherit `api_router`'s three rungs, so a route
    # does not lose its gate by moving.
    management = getattr(_module, "management_router", None)
    if management is not None:
        api_router.include_router(management)
