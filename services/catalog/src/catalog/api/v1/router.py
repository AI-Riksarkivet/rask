"""Aggregate all v1 endpoint routers."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from catalog.api.delimiter import DelimiterGuard
from catalog.api.fga_deps import authorize
from catalog.api.v1.endpoints import (
    access,
    access_admin,
    branches,
    columns,
    credentials,
    data,
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
    transforms,
    user_state,
    versions,
    views,
    warehouses,
)


# Router-level authn + authz (via authorize, which composes the OIDC token):
# a no-op when both are disabled, enforced per route when enabled.
#
# `DelimiterGuard` rides here for the same reason `authorize` does: it must hold for every route,
# including the next one added. The spec's `delimiter` was declared on 0 of 153 served operations, so
# a client configured with a different one had every multi-segment identifier silently re-split with
# the server's — see `catalog.api.delimiter` for why this refuses rather than honours.
api_router = APIRouter(dependencies=[Depends(authorize), DelimiterGuard])
for _module in (
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
    events,
    projects,
    me,
    maintenance,
    members,
    publication,
    user_state,
    stores,
    gates,
    transforms,
):
    api_router.include_router(_module.router)
