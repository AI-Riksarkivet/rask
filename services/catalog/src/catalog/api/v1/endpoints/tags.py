"""Tag endpoints (implemented via the pylance data plane)."""

from __future__ import annotations

from fastapi import APIRouter
from lance_namespace import (
    CreateTableTagRequest,
    CreateTableTagResponse,
    DeleteTableTagRequest,
    DeleteTableTagResponse,
    GetTableTagVersionRequest,
    GetTableTagVersionResponse,
    ListTableTagsRequest,
    ListTableTagsResponse,
    UpdateTableTagRequest,
    UpdateTableTagResponse,
)

from catalog.api.dependencies import NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.services import dataplane


router = APIRouter(prefix="/v1/table", tags=["tag"])


@router.post("/{id}/tags/list", response_model_exclude_none=True)
def list_table_tags(id: str, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep) -> ListTableTagsResponse:
    """List every tag on the table — wraps lance_namespace ListTableTags."""
    req = ListTableTagsRequest(id=parse_identifier(id, settings.delimiter))
    return dataplane.list_tags(ns, so, req)


@router.post("/{id}/tags/create", response_model_exclude_none=True)
def create_table_tag(id: str, body: CreateTableTagRequest, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep) -> CreateTableTagResponse:
    """Tag the given table version with a name — wraps lance_namespace CreateTableTag."""
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    return dataplane.create_tag(ns, so, body)


@router.post("/{id}/tags/version", response_model_exclude_none=True)
def get_table_tag_version(
    id: str, body: GetTableTagVersionRequest, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep
) -> GetTableTagVersionResponse:
    """Resolve which table version a tag points to — wraps lance_namespace GetTableTagVersion."""
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    return dataplane.get_tag_version(ns, so, body)


@router.post("/{id}/tags/update", response_model_exclude_none=True)
def update_table_tag(id: str, body: UpdateTableTagRequest, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep) -> UpdateTableTagResponse:
    """Move an existing tag to a new table version — wraps lance_namespace UpdateTableTag."""
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    return dataplane.update_tag(ns, so, body)


@router.post("/{id}/tags/delete", response_model_exclude_none=True)
def delete_table_tag(id: str, body: DeleteTableTagRequest, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep) -> DeleteTableTagResponse:
    """Delete a tag from the table — wraps lance_namespace DeleteTableTag."""
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    return dataplane.delete_tag(ns, so, body)
