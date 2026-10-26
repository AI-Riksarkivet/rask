"""Transaction endpoints: a transaction is named ``<table id>$<transaction>`` and answered on that table ([[LH-221]])."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    AlterTransactionRequest,
    AlterTransactionResponse,
    DescribeTransactionRequest,
    DescribeTransactionResponse,
    UnsupportedOperationError,
)

from catalog.api.dependencies import ResolverDep, TransactionTableDep
from catalog.core.identifiers import parse_identifier, reconcile_body_id, transaction_table
from service_kit.lakehouse import trash


router = APIRouter(prefix="/v1/transaction", tags=["transaction"])


@router.post("/{id}/describe", response_model_exclude_none=True)
async def describe_transaction(
    id: str, door: TransactionTableDep, resolver: ResolverDep, body: DescribeTransactionRequest | None = None
) -> DescribeTransactionResponse:
    """Report the status of transaction ``id`` of the table it names, read by the backend's ``describe_transaction`` on that table.

    A recoverable drop's transaction (``<table id>$<the dropped table's uuid>``, ADR 0146) is answered from its trash
    record: ``Queued`` while the drop can be undone, ``Running`` past its deadline, when its grants are released and its
    bytes wait for the purge. Once the purge or an undrop clears the record, the id names nothing.
    """
    segments = reconcile_body_id(parse_identifier(id), body.id if body else None)
    if await resolver.trashed_as_transaction(segments):
        admitted = await resolver.admitted_trash("table", transaction_table(segments))
        record = next(r for r in admitted if trash.object_uuid(r) == segments[-1])
        final = trash.past_deadline(record) or trash.is_released(record)
        return DescribeTransactionResponse(
            status="Running" if final else "Queued",
            properties={"operation": "drop_table", "dropped_at": str(record.get("dropped_at", "")), "expires_at": str(record.get("expires_at", ""))},
        )
    return await run_in_threadpool(door.read_natively, "describe_transaction", DescribeTransactionRequest(id=segments))


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
