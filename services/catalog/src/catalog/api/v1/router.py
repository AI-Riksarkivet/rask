"""Aggregate all v1 endpoint routers."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from catalog.api.fga_deps import authorize, require_request_names
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
# caller may make it (403, `authorize`; a no-op when authentication and authorization are both off). Router-level, so
# the next route added inherits all three.
api_router = APIRouter(dependencies=[Depends(authenticate), Depends(require_request_names), Depends(authorize)])
#
# One rule for every endpoint module: it exports its routers by name, each carrying its own prefix, and this list mounts
# them. A `spec_router` serves the Lance Namespace spec's doors under `/v1`; every other router serves rask's own under
# `/management/v1` ([[LH-021]]: a spec client meeting a rask verb on a spec prefix could not tell the two apart). A
# router this list does not name is mounted nowhere, and a name it gets wrong fails the import.
for _router in (
    erasure.table_router,
    erasure.obligations_router,
    namespaces.spec_router,
    namespaces.management_router,
    tables.spec_router,
    tables.management_router,
    data.spec_router,
    data.management_router,
    columns.spec_router,
    indices.spec_router,
    tags.spec_router,
    branches.spec_router,
    versions.spec_router,
    versions.management_router,
    transactions.spec_router,
    views.spec_router,
    credentials.management_router,
    warehouses.management_router,
    policies.table_router,
    policies.namespace_router,
    policies.project_router,
    policies.projects_router,
    labels.table_router,
    labels.namespace_router,
    labels.projects_router,
    contracts.management_router,
    access.table_router,
    access.namespace_router,
    access.warehouse_router,
    access.project_router,
    access.classification_router,
    access_admin.management_router,
    decisions.management_router,
    events.management_router,
    projects.management_router,
    me.management_router,
    maintenance.table_router,
    maintenance.due_router,
    members.management_router,
    roles.management_router,
    grant_reconcile.management_router,
    trash_reclaim.management_router,
    clone_endings.management_router,
    idempotency_keys.management_router,
    announcement_intents.management_router,
):
    api_router.include_router(_router)
