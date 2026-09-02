"""Transaction endpoints (delegated to the native backend)."""

from __future__ import annotations

from fastapi import APIRouter
from lance_namespace import (
    AlterTransactionRequest,
    AlterTransactionResponse,
    DescribeTransactionRequest,
    DescribeTransactionResponse,
)

from catalog.api.dependencies import NamespaceDep, SettingsDep
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.services import native


router = APIRouter(prefix="/v1/transaction", tags=["transaction"])


@router.post("/{id}/describe", response_model_exclude_none=True)
def describe_transaction(id: str, ns: NamespaceDep, settings: SettingsDep, body: DescribeTransactionRequest | None = None) -> DescribeTransactionResponse:
    """Report the current status of transaction ``id`` — wraps the backend ``describe_transaction`` op."""
    req = DescribeTransactionRequest(id=reconcile_body_id(parse_identifier(id, settings.delimiter), body.id if body else None))
    return native.call(ns, "describe_transaction", req)


@router.post("/{id}/alter", response_model_exclude_none=True)
def alter_transaction(id: str, body: AlterTransactionRequest, ns: NamespaceDep, settings: SettingsDep) -> AlterTransactionResponse:
    """Apply the requested state actions to transaction ``id`` — backend ``alter_transaction``."""
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    return native.call(ns, "alter_transaction", body)
