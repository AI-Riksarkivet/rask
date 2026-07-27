"""Table branch endpoints — backed in-process via the pylance data plane.

The native ``DirectoryNamespace`` 501s branch ops, but ``lance.LanceDataset`` implements Git-like branches
(``ds.branches`` / ``ds.create_branch``), so we back them in-process here exactly like tags — turning the
former spec-correct 501 into a real, working operation.
"""

from __future__ import annotations

from fastapi import APIRouter
from lance_namespace import (
    CreateTableBranchRequest,
    CreateTableBranchResponse,
    DeleteTableBranchRequest,
    DeleteTableBranchResponse,
    ListTableBranchesRequest,
    ListTableBranchesResponse,
)

from catalog.api.dependencies import NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.services import dataplane


router = APIRouter(prefix="/v1/table", tags=["branch"])


@router.post("/{id}/branches/list", response_model_exclude_none=True)
def list_table_branches(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    page_token: str | None = None,
    limit: int | None = None,
) -> ListTableBranchesResponse:
    """List a table's Git-like branches (paginated) — wraps the pylance ``list_branches`` data-plane op."""
    req = ListTableBranchesRequest(id=parse_identifier(id, settings.delimiter), page_token=page_token, limit=limit)
    return dataplane.list_branches(ns, so, req)


@router.post("/{id}/branches/create", response_model_exclude_none=True)
def create_table_branch(id: str, body: CreateTableBranchRequest, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep) -> CreateTableBranchResponse:
    """Create a branch from main (or a source branch/version) — wraps pylance ``create_branch``."""
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    return dataplane.create_branch(ns, so, body)


@router.post("/{id}/branches/delete", response_model_exclude_none=True)
def delete_table_branch(id: str, body: DeleteTableBranchRequest, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep) -> DeleteTableBranchResponse:
    """Delete a branch from the table — wraps the pylance ``delete_branch`` data-plane op."""
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    return dataplane.delete_branch(ns, so, body)
