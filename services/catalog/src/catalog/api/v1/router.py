"""Aggregate all v1 endpoint routers."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from catalog.api.fga_deps import authorize, parent_fence, require_request_names
from catalog.api.security import authenticate
from catalog.api.v1.endpoints import (
    access,
    access_admin,
    announcement_intents,
    branches,
    clone_endings,
    columns,
    contracts,
    credentials,
    data,
    decisions,
    erasure,
    events,
    grant_reconcile,
    idempotency_keys,
    indices,
    labels,
    maintenance,
    me,
    members,
    namespaces,
    policies,
    projects,
    roles,
    tables,
    tags,
    transactions,
    trash_reclaim,
    versions,
    views,
    warehouses,
)


# Every route passes three rungs before its door runs, in the order every door judges a request: who is asking (401,
# `authenticate`), whether the request names only what a door can read (400, `require_request_names`), and whether the
# caller may make it (403, `authorize`; a no-op when authentication and authorization are both off). Then the fence a
# namespace created in a warehouse is seeded under is opened and the gate asked again on the incarnation it read
# (`parent_fence`, ADR 0241): after the gate, so a caller refused writes nothing. Router-level, so the next route added
# inherits all of them.
api_router = APIRouter(dependencies=[Depends(authenticate), Depends(require_request_names), Depends(authorize), Depends(parent_fence)])
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
    policies,
    labels,
    contracts,
    access,
    access_admin,
    decisions,
    events,
    projects,
    me,
    maintenance,
    members,
    roles,
    grant_reconcile,
    trash_reclaim,
    clone_endings,
    idempotency_keys,
    announcement_intents,
):
    api_router.include_router(_module.router)
    # A module may ALSO carry a `management_router`, mounted at `/management/v1/...`. That is the seam
    # [[LH-021]] moves rask-only operations across: a spec client meeting a rask verb on a spec prefix
    # has no way to tell the two apart, so the estate's own governance gets its own mount, `/management`, apart from
    # the spec's doors. Both routers inherit `api_router`'s three rungs, so a route
    # does not lose its gate by moving.
    management = getattr(_module, "management_router", None)
    if management is not None:
        api_router.include_router(management)
