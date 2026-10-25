"""Transaction endpoints: a transaction is named ``<table id>$<transaction>`` and answered on that table ([[LH-221]])."""

from __future__ import annotations

from fastapi import APIRouter
from lance_namespace import (
    AlterTransactionRequest,
    AlterTransactionResponse,
    DescribeTransactionRequest,
    DescribeTransactionResponse,
    UnsupportedOperationError,
)

from catalog.api.dependencies import TransactionTableDep
from catalog.core.identifiers import parse_identifier, reconcile_body_id, transaction_table


router = APIRouter(prefix="/v1/transaction", tags=["transaction"])


@router.post("/{id}/describe", response_model_exclude_none=True)
def describe_transaction(id: str, door: TransactionTableDep, body: DescribeTransactionRequest | None = None) -> DescribeTransactionResponse:
    """Report the status of transaction ``id`` of the table it names, read by the backend's ``describe_transaction`` on that table."""
    req = DescribeTransactionRequest(id=reconcile_body_id(parse_identifier(id), body.id if body else None))
    return door.read_natively("describe_transaction", req)


@router.post("/{id}/alter", response_model_exclude_none=True)
def alter_transaction(id: str, body: AlterTransactionRequest) -> AlterTransactionResponse:
    """Decline every alteration: the spec's alter applies all its actions or none, and the backend applies none.

    On pylance 13.0.0 the `dir` backend answers SUCCEEDED to a status change and to a property and applies neither, so a
    200 would report an alteration that never happened.

    Raises:
        InvalidInputError: The id names no table, or the body's id is another.
        UnsupportedOperationError: Always, past the shape and authorization gates.
    """
    transaction_table(reconcile_body_id(parse_identifier(id), body.id))
    raise UnsupportedOperationError("AlterTransaction is not supported: the backend applies no transaction alteration")
