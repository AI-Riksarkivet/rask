"""A table's data contract: set, read and deleted at its own doors (ADR 0212).

Each door is rask's own, so it mounts at ``/management/v1`` and inherits the three rungs `api/v1/router.py` gives every
route. Addressed by name as every governed door is, and the contract is kept under the table's uuid:

- ``…/contract/describe`` is a metadata read, and the router asks ``can_get_metadata``;
- ``…/contract/set`` and ``…/contract/delete`` decide what the table's commits may do to its schema, and the router
  asks ``can_set_contract``, the owner's act (`catalog.api.fga_deps`). Each change is announced as an untargeted
  control event.

The contract is checked where every commit passes (`catalog.services.contracts`), and nowhere else: these doors hold no
version, scan no row and run no timer. A set commits one version on each ref, which changes nothing, so that every
commit is ordered with it (ADR 0244).
"""

from __future__ import annotations

from functools import partial
from typing import Annotated, Any

import lance
from fastapi import APIRouter, Body, Header
from fastapi.concurrency import run_in_threadpool
from lance.dataset import LanceOperation, Transaction
from lance_namespace import ConcurrentModificationError

from catalog.api import lineage_deps
from catalog.api.dependencies import ControlEmitterDep, DoorTableDep, LineageEmitterDep, ObjectUuidsDep, SettingsDep, StorageOptionsDep
from catalog.api.security import Caller, CurrentToken
from catalog.core.config import Settings
from catalog.core.lineage_emit import UPDATE_SCHEMA_METADATA
from catalog.schemas import ContractResponse
from catalog.services import contracts, dataplane
from catalog.services.table_claims import DoorTable
from lakehouse import records
from lakehouse.control_emit import emit_control
from lakehouse.governed import fga
from lakehouse.storage_options import StorageOptions


router = APIRouter(prefix="/management/v1/table", tags=["contract"])


def _actor(token: Caller | None) -> str | None:
    return f"user:{token.subject}" if token is not None else None


def _answer(table: DoorTable, record: contracts.ContractRecord | None) -> ContractResponse:
    return ContractResponse(
        table=fga.canonical_object_id(table.segments),
        uuid=table.resolved.require_uuid(),
        contract=record.document if record is not None else None,
        set_by=record.set_by if record is not None else None,
        set_at=record.set_at if record is not None else None,
        in_force=record.in_force if record is not None else None,
    )


def _held(settings: Settings, table: DoorTable) -> contracts.ContractRecord | None:
    # The record is kept under the uuid the router authorized, so the door first asks that the name still holds it.
    table.require_held()
    return contracts.held(settings.registry_root, settings.storage_options(), table.resolved.require_uuid())


def _standing(settings: Settings, table: DoorTable) -> contracts.ContractRecord | None:
    """As :func:`_held`, for the doors that replace or remove the record: one that cannot be read is theirs to end."""
    table.require_held()
    return contracts.standing(settings.registry_root, settings.storage_options(), table.resolved.require_uuid())


def _branches(table: DoorTable, so: StorageOptions) -> list[str]:
    return sorted(table.open_unchecked(so).branches.list())


def _require_described(table: DoorTable, so: StorageOptions, contract: contracts.Contract) -> None:
    """Refuse a contract the table does not meet now, on main or on a branch, before its record is written.

    Raises:
        InvalidTableStateError: A ref's schema lacks a column the contract names, or holds it at another type or looser.
    """
    contracts.require_met(contract, table.open_unchecked(so).schema, ref="main")
    for name in _branches(table, so):
        contracts.require_met(contract, table.open_unchecked(so, branch=name).schema, ref=name)


def _verify_on_every_ref(
    table: DoorTable, so: StorageOptions, contract: contracts.Contract, properties: dict[str, str], landed: list[tuple[str | None, int]]
) -> None:
    """Commit on main and on every branch, as listed once the record stands, a version that changes nothing, whose plan
    judges the ref's head against ``contract``, each appended to ``landed`` as it lands (ADR 0244 C1).

    The version is a stamped commit, finalized exactly at the slot after the one it planned on: a commit that read no
    contract and planned on that version loses its slot to it and reads the contract when it plans again, and one that
    landed first is in the head this plan judges. A second read of each ref could not order such a commit: it lands
    after the read as well as before. What orders it is the slot alone, so the version is an ``UpdateConfig`` that
    updates nothing: it carries no key a restore to an older version would have to keep or a stock client to carry
    (ADR 0244 W3; measured on pylance 13.0.0, it lands a version whose schema metadata and config are its parent's).

    Raises:
        InvalidTableStateError: A ref does not meet the contract: a commit landed since the door's first read.
        ConcurrentModificationError: Every attempt of a ref's commit lost its race.
    """
    for ref in [None, *_branches(table, so)]:

        def plan(dataset: lance.LanceDataset, ref: str | None = ref) -> tuple[Transaction, None]:
            contracts.require_met(contract, dataset.schema, ref=ref or "main")
            return Transaction(read_version=dataset.version, operation=LanceOperation.UpdateConfig()), None

        version, _ = dataplane.commit_stamped(table, so, properties, plan, branch=ref)
        landed.append((ref, version))


@router.post("/{id}/contract/describe")
async def describe_table_contract(id: str, table: DoorTableDep, settings: SettingsDep) -> ContractResponse:
    """The data contract the table holds, as the ODCS v3 document its owner set; ``contract`` is null for a table
    holding none. Read at ``can_get_metadata``."""
    return _answer(table, await run_in_threadpool(_held, settings, table))


@router.post("/{id}/contract/set")
async def set_table_contract(
    id: str,
    document: Annotated[dict[str, Any], Body()],
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> ContractResponse:
    """Set the table's data contract: an ODCS v3 document naming the columns its consumers rely on. Asked at
    ``can_set_contract``, the table owner's.

    The document is read strictly: a quality or service-level rule is refused 406, since the catalog checks a
    contract's schema on every commit and nothing else. The table must meet the contract now, and a contract that takes
    back what the standing one promises carries a higher major version. The record is written in flight: until the set
    answers, a commit is held to it and to the contract in force before it, and ``describe`` shows it with ``in_force``
    false. Once the record stands the door commits on main and on every branch a version that changes nothing, each
    announced as a schema metadata update; a ref a commit made stop meeting the contract meanwhile refuses the set 409
    (code 19), and the door then puts back the contract in force it replaced. A set whose record another set or the
    delete door replaced before it took effect is refused 409 (code 14). From the answer on, a commit that drops,
    renames, retypes or loosens a contracted column is refused 409 (code 19), through every door and from a stock client
    alike.
    """
    contract = contracts.parsed(document)
    await run_in_threadpool(_require_described, table, so, contract)
    uuid = table.resolved.require_uuid()
    root, control_so = settings.registry_root, settings.storage_options()
    # The record is kept under the uuid the router authorized, so the door first asks that the name still holds it.
    await run_in_threadpool(table.require_held)
    try:
        record = await run_in_threadpool(partial(contracts.keep, root, control_so, uuid, contract, document=document, actor=_actor(token)))
    except (records.RecordChangedError, records.RecordMissingError) as exc:
        raise ConcurrentModificationError(f"the contract of {fga.canonical_object_id(table.segments)} kept changing; set it again") from exc
    landed: list[tuple[str | None, int]] = []
    try:
        await run_in_threadpool(_verify_on_every_ref, table, so, contract, lineage_deps.door_stamp(token, UPDATE_SCHEMA_METADATA), landed)
    except BaseException:
        await run_in_threadpool(contracts.take_back, root, control_so, record)
        raise
    finally:
        for ref, version in landed:
            await lineage_deps.emit_measured_write(
                emitter,
                table.segments,
                location=table.location,
                so=so,
                token=token,
                operation=UPDATE_SCHEMA_METADATA,
                authorization=authorization,
                uuids=uuids,
                pin_version=version,
                branch=ref,
            )
    confirmed = await run_in_threadpool(contracts.confirm, root, control_so, record)
    if confirmed is None:
        raise ConcurrentModificationError(
            f"another set or the contract's delete replaced this contract of {fga.canonical_object_id(table.segments)} before it took effect; "
            "what that door wrote stands: read it with describe, and set this contract again"
        )
    await emit_control(
        control,
        action="table_contract_set",
        object_type="table",
        object_id=fga.table_object(uuid),
        object_name=fga.canonical_object_id(table.segments),
        actor=_actor(token),
        extra={"contract": contract.id, "version": contract.version},
    )
    return _answer(table, confirmed)


@router.post("/{id}/contract/delete")
async def delete_table_contract(id: str, table: DoorTableDep, settings: SettingsDep, token: CurrentToken, control: ControlEmitterDep) -> ContractResponse:
    """Delete the table's data contract (idempotent): its commits are held to no contract after. Asked at
    ``can_set_contract``."""
    uuid = table.resolved.require_uuid()
    standing = await run_in_threadpool(_standing, settings, table)
    ended = standing.contract() if standing is not None else None
    if await run_in_threadpool(contracts.forget, settings.registry_root, settings.storage_options(), uuid) and ended is not None:
        await emit_control(
            control,
            action="table_contract_deleted",
            object_type="table",
            object_id=fga.table_object(uuid),
            object_name=fga.canonical_object_id(table.segments),
            actor=_actor(token),
            extra={"contract": ended.id, "version": ended.version},
        )
    return _answer(table, None)
