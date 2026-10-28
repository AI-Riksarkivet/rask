"""The catalog's own record of every table each writing table's commits declared they read ([[LH-541]], ADR 0183).

A run names its inputs in its commit (``rask.inputs``, ADR 0156 C2), and the catalog resolves each to the uuid it names,
under the committer's grant, before the commit is finalized (``catalog.api.fga_deps.require_declared_inputs``). This
record keeps what that resolution found: for each writing table, keyed by its uuid, every input uuid its commits declared,
with when the catalog first recorded it and the version and branch of the input that declaration named. The writing
commit's own version is not known before a door commits (``dataplane.commit_stamped`` plans it on the ref's latest version
and plans again when another writer takes it), so the record names the input's, which a commit names before it lands.

**WHY THE CATALOG KEEPS ITS OWN.** An erasure names the tables derived from the erased one (ADR 0178), and lineage's edge
list records a derived table only once it consumes the catalog's announcement of that table's write, so a table written
while lineage is down or behind is not on it yet. This record is written before the commit lands, so the verification
reads a derived table the moment it is committed, whether lineage is up or not. The commit's transaction is no substitute:
Lance documents it only for commit retries and conflict detection, its inline copy is "an optional cache optimization"
(``lance_docs/lance/format/table/transaction.md`` § Transaction Files, § Inline Transaction Sections), and cleanup removes
old version metadata (``lance_docs/lance/guide/read_and_write.md`` § Cleanup old versions).

**ONE RECORD PER WRITING TABLE** (``_table_inputs/table-<digest>.json``), created put-if-not-exists and changed only under
the ETag it was read with, so two commits declaring at once lose neither's input. The time is the object store's clock at
the read (``records.StoreRead.at``, ADR 0179). A record is not ended with its table, as the edge list is never pruned: a
deregistered table's bytes may still hold the subject, and a gone one costs a verification one claim lookup.

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from functools import partial
from typing import Any, Final

from lance_namespace import InternalError
from pydantic import BaseModel, ConfigDict, ValidationError

from catalog.core.run_context import RunInput
from service_kit.lakehouse import derived_tables
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import list_records, record_key
from service_kit.lakehouse.records import RecordChangedError, RecordExistsError, create_json, read_json_at, replace_json


PREFIX: Final = "_table_inputs"

#: How often a record write reads again after another commit's write landed between its read and its write.
_ATTEMPTS: Final = 8


class DeclaredInput(BaseModel):
    """One input a writing table's commits declared, as the catalog first recorded it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    first_declared_at: datetime
    version: int
    branch: str | None = None


class TableInputs(BaseModel):
    """Every input the commits of one writing table declared."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table_uuid: str
    #: The writing table's name at its latest declaring commit, which a verification's detail names it by.
    name: str
    #: Each input's uuid, with its first declaration.
    inputs: dict[str, DeclaredInput] = {}


def _key(table_uuid: str) -> str:
    return record_key(PREFIX, "table", table_uuid)


def record(control_root: str, storage_options: StorageOptions, table_uuid: str, *, name: str, declared: Mapping[str, RunInput]) -> None:
    """Record that a commit of the table ``table_uuid``, named ``name``, declared the inputs ``declared`` (uuid to input).

    An input already recorded keeps its first declaration. Called before the commit is finalized, so a commit that then
    fails leaves an input recorded that no version declares, which names one table too many to an erasure.

    Raises:
        InternalError: The table's record does not read as one, or another write kept landing under every attempt.
        OSError: The store refused the read or the write.
    """
    key = _key(table_uuid)
    for _ in range(_ATTEMPTS):
        read = read_json_at(control_root, storage_options, key)
        current = TableInputs(table_uuid=table_uuid, name=name) if read.stored is None else _parsed(read.stored.record, key=key)
        added = {
            input_uuid: DeclaredInput(first_declared_at=read.at, version=entry.version, branch=entry.branch)
            for input_uuid, entry in declared.items()
            if input_uuid not in current.inputs
        }
        if not added and current.name == name:
            return
        updated = TableInputs(table_uuid=table_uuid, name=name, inputs={**current.inputs, **added}).model_dump(mode="json")
        try:
            if read.stored is None:
                create_json(control_root, storage_options, key, updated)
            else:
                replace_json(control_root, storage_options, key, updated, etag=read.stored.etag)
        except (RecordExistsError, RecordChangedError):
            continue
        return
    raise InternalError(f"the input record of table {table_uuid} kept changing across {_ATTEMPTS} attempts; the commit is not made")


def recorder(control_root: str, storage_options: StorageOptions, *, name: str, declared: Mapping[str, RunInput]) -> Callable[[str], None] | None:
    """What a create hands its write to record ``declared`` for the table it writes, once that table's uuid is held;
    ``None`` when the commit declares no input."""
    if not declared:
        return None
    return partial(record, control_root, storage_options, name=name, declared=declared)


def _parsed(raw: dict[str, Any], *, key: str) -> TableInputs:
    try:
        return TableInputs.model_validate(raw)
    except ValidationError as exc:
        raise InternalError(f"the input record {key} does not read as one: {exc.errors(include_url=False)[0]['msg']}") from exc


def every_record(control_root: str, storage_options: StorageOptions) -> list[TableInputs]:
    """Every writing table's record, read whole.

    Raises:
        InternalError: A record does not read as one, which may be the one naming a derived table.
        ValueError: A record is not a JSON object.
        OSError: The store could not be listed, or a record read.
    """
    return [_parsed(raw, key=PREFIX) for raw in list_records(control_root, storage_options, PREFIX, event="table_inputs", strict=True)]


def as_edges(records: Sequence[TableInputs]) -> derived_tables.EdgeRows:
    """``records`` read as the walk's edges (:mod:`service_kit.lakehouse.derived_tables`): one per declared input, received
    when it was first declared."""
    by_input: dict[str, list[dict[str, Any]]] = {}
    for entry in records:
        for input_uuid, declared in entry.inputs.items():
            by_input.setdefault(input_uuid, []).append({"out_table": entry.table_uuid, "out_name": entry.name, "received_at": declared.first_declared_at})
    return lambda inputs: [row for key in inputs for row in by_input.get(key, ())]
