"""A table's data contract: set, read and deleted at its own doors (ADR 0212).

Each door is rask's own, so it mounts at ``/management/v1`` and inherits the three rungs `api/v1/router.py` gives every
route. Addressed by name as every governed door is, and the contract is kept under the table's uuid:

- ``…/contract/describe`` is a metadata read, and the router asks ``can_get_metadata``;
- ``…/contract/set`` and ``…/contract/delete`` decide what the table's commits may do to its schema, and the router
  asks ``can_set_contract``, the owner's act (`catalog.api.fga_deps`). Each change is announced as an untargeted
  control event.

The contract is checked where every commit passes (`catalog.services.contracts`), and nowhere else: these doors hold no
version, scan no row and run no timer.
"""

from __future__ import annotations

from functools import partial
from typing import Annotated, Any

from fastapi import APIRouter, Body
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ConcurrentModificationError, InvalidTableStateError

from catalog.api.dependencies import ControlEmitterDep, DoorTableDep, SettingsDep, StorageOptionsDep
from catalog.api.security import Caller, CurrentToken
from catalog.core.config import Settings
from catalog.schemas import ContractResponse
from catalog.services import contracts
from catalog.services.table_claims import DoorTable
from lakehouse import records
from lakehouse.control_emit import emit_control
from lakehouse.governed import fga
from lakehouse.storage_options import StorageOptions


management_router = APIRouter(prefix="/management/v1/table", tags=["contract"])


def _actor(token: Caller | None) -> str | None:
    return f"user:{token.subject}" if token is not None else None


def _answer(table: DoorTable, record: contracts.ContractRecord | None) -> ContractResponse:
    return ContractResponse(
        table=fga.canonical_object_id(table.segments),
        uuid=table.resolved.require_uuid(),
        contract=record.document if record is not None else None,
        set_by=record.set_by if record is not None else None,
        set_at=record.set_at if record is not None else None,
    )


def _held(settings: Settings, table: DoorTable) -> contracts.ContractRecord | None:
    # The record is kept under the uuid the router authorized, so the door first asks that the name still holds it.
    table.require_held()
    return contracts.held(settings.registry_root, settings.storage_options(), table.resolved.require_uuid())


def _standing(settings: Settings, table: DoorTable) -> contracts.ContractRecord | None:
    """As :func:`_held`, for the doors that replace or remove the record: one that cannot be read is theirs to end."""
    table.require_held()
    return contracts.standing(settings.registry_root, settings.storage_options(), table.resolved.require_uuid())


def _require_described(table: DoorTable, so: StorageOptions, contract: contracts.Contract) -> None:
    """Refuse a contract the table does not meet now, on main or on a branch: every later commit would be refused for a
    state its writer did not make.

    Raises:
        InvalidTableStateError: A ref's schema lacks a column the contract names, or holds it at another type or looser.
    """
    main = table.open_unchecked(so)
    for ref, dataset in [("main", main), *((name, table.open_unchecked(so, branch=name)) for name in sorted(main.branches.list()))]:
        if broken := contracts.violations(contract, dataset.schema):
            raise InvalidTableStateError(
                f"the table does not meet the contract on {ref}: {'; '.join(broken)}. A contract states what the table already is; "
                "change the table first (a column made non-nullable, a key declared at create), then set the contract"
            )


@management_router.post("/{id}/contract/describe")
async def describe_table_contract(id: str, table: DoorTableDep, settings: SettingsDep) -> ContractResponse:
    """The data contract the table holds, as the ODCS v3 document its owner set; ``contract`` is null for a table
    holding none. Read at ``can_get_metadata``."""
    return _answer(table, await run_in_threadpool(_held, settings, table))


@management_router.post("/{id}/contract/set")
async def set_table_contract(
    id: str,
    document: Annotated[dict[str, Any], Body()],
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
) -> ContractResponse:
    """Set the table's data contract: an ODCS v3 document naming the columns its consumers rely on. Asked at
    ``can_set_contract``, the table owner's.

    The document is read strictly: a quality or service-level rule is refused 406, since the catalog checks a
    contract's schema on every commit and nothing else. The table must meet the contract now, and a contract that takes
    back what the standing one promises carries a higher major version. From then on a commit that drops, renames,
    retypes or loosens a contracted column is refused 409 (code 19), through every door and from a stock client alike.
    """
    contract = contracts.parsed(document)
    await run_in_threadpool(_require_described, table, so, contract)
    uuid = table.resolved.require_uuid()
    root, control_so = settings.registry_root, settings.storage_options()
    replaced = await run_in_threadpool(_standing, settings, table)
    try:
        record = await run_in_threadpool(partial(contracts.keep, root, control_so, uuid, contract, document=document, actor=_actor(token)))
    except (records.RecordChangedError, records.RecordMissingError) as exc:
        raise ConcurrentModificationError(f"the contract of {fga.canonical_object_id(table.segments)} kept changing; set it again") from exc
    try:
        # Asked again now the record stands, so a commit that landed while the table was first read is seen: from the
        # record on, a commit is judged against it. A commit that read no contract and lands after this leaves a
        # violation no later commit is refused for (`contracts.require_kept`), which `describe` and the table show.
        await run_in_threadpool(_require_described, table, so, contract)
    except BaseException:
        await run_in_threadpool(contracts.restore, root, control_so, uuid, replaced)
        raise
    await emit_control(
        control,
        action="table_contract_set",
        object_type="table",
        object_id=fga.table_object(uuid),
        object_name=fga.canonical_object_id(table.segments),
        actor=_actor(token),
        extra={"contract": contract.id, "version": contract.version},
    )
    return _answer(table, record)


@management_router.post("/{id}/contract/delete")
async def delete_table_contract(id: str, table: DoorTableDep, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep) -> ContractResponse:
    """Delete the table's data contract (idempotent): its commits are held to no contract after. Asked at
    ``can_set_contract``."""
    uuid = table.resolved.require_uuid()
    standing = await run_in_threadpool(_standing, settings, table)
    if await run_in_threadpool(contracts.forget, settings.registry_root, settings.storage_options(), uuid) and standing is not None:
        await emit_control(
            control,
            action="table_contract_deleted",
            object_type="table",
            object_id=fga.table_object(uuid),
            object_name=fga.canonical_object_id(table.segments),
            actor=_actor(token),
            extra={"contract": standing.contract().id, "version": standing.contract().version},
        )
    return _answer(table, None)
