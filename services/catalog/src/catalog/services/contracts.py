"""A table's data contract: the columns its consumers rely on, held to on every commit (ADR 0212).

A contract is an ODCS v3 document (Open Data Contract Standard, Bitol) its table's owner sets. The catalog keeps the
document and enforces the part of it a commit can break, the schema: each property the contract names is a column the
table keeps, at the type the contract states, non-nullable where the contract says ``required``. Nothing else of ODCS
is enforced, so nothing else is accepted: a document carrying a quality rule or a service-level property is refused as
unsupported, since the catalog scans no data and runs no timer, and a contract must not promise a check nobody runs.

What each ODCS field becomes:

- ``required: true`` is a non-nullable Lance field. Lance refuses a fragment whose schema differs from the table's,
  nullability included, so the rows are held to it by Lance itself; the catalog holds the schema to it.
- ``primaryKey: true`` is a field of Lance's unenforced primary key (`lance_docs/lance/format/table/index.md`
  § Unenforced Primary Key), which Lance fixes once set and whose repeats every commit is already refused for
  (`catalog.services.primary_keys`). ``unique`` is accepted only on such a field.
- ``physicalType`` is the column's Arrow type as pyarrow spells it (``int64``, ``string``, ``timestamp[us, tz=UTC]``).
  A property with nested ``properties`` is a struct, judged child by child. ``logicalType``, where stated, is held to
  the family that type belongs to (``integer`` for ``int64``), so it is no claim the catalog leaves unchecked.

A column is named by its path, so a rename of a contracted column reads as its removal. A change the contract forbids
is made by setting a contract that no longer forbids it, under a higher major version, and then committing the change.

A COMMIT IS REFUSED FOR WHAT IT BREAKS. :func:`require_kept` compares the version a commit makes with the one it
follows and refuses a violation the commit introduces. A violation the table already had (a contract set as a commit
landed, a branch cut from a version older than the contract) is not the next writer's: its commits land, and the
owner mends the table or the contract.

One record per table, at ``record_key("_contracts", "table", uuid)``: keyed by the table's uuid as every record kept
for an object is (ADR 0173 §8), so a rename keeps it and the table's ending ends it (`lakehouse.object_records`).

All IO here is blocking; callers threadpool it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, Final, Literal

import pyarrow as pa
from lance_namespace import InternalError, InvalidInputError, InvalidTableStateError, UnsupportedOperationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from catalog.services import primary_keys
from lakehouse import control_prefixes
from lakehouse.outage import store_refusal
from lakehouse.record_store import delete_record, put_record, read_record, record_key
from lakehouse.records import RecordExistsError, RecordMissingError, create_json, mutate_json
from lakehouse.storage_options import StorageOptions


#: The ODCS keys that state a check on data or on time. The catalog runs neither, so a document carrying one is refused.
UNENFORCED_KEYS: Final = frozenset({"quality", "slaProperties", "slaDefaultElement"})

_SEMVER: Final = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

type LogicalType = Literal["string", "date", "timestamp", "time", "number", "integer", "object", "array", "boolean"]


class Property(BaseModel):
    """One column the contract names: an ODCS schema property, the fields a commit can be held to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=255)
    logicalType: LogicalType | None = None  # noqa: N815 — ODCS's own field name
    physicalType: str | None = Field(default=None, min_length=1, max_length=512)  # noqa: N815 — ODCS's own field name
    description: str | None = Field(default=None, max_length=4096)
    required: bool = False
    primaryKey: bool = False  # noqa: N815 — ODCS's own field name
    primaryKeyPosition: int | None = Field(default=None, ge=1)  # noqa: N815 — ODCS's own field name
    unique: bool = False
    properties: tuple[Property, ...] = ()

    @model_validator(mode="after")
    def _states_a_type(self) -> Property:
        if self.properties:
            if self.physicalType not in (None, "struct"):
                raise ValueError(f"property {self.name!r} has nested properties, so its physicalType is `struct` or absent")
        elif self.physicalType is None:
            raise ValueError(f"property {self.name!r} states no physicalType: the column's Arrow type, such as `int64` or `string`")
        if self.primaryKeyPosition is not None and not self.primaryKey:
            raise ValueError(f"property {self.name!r} gives a primaryKeyPosition and is no primaryKey")
        return self


class SchemaObject(BaseModel):
    """The contract's one schema object: the table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=255)
    physicalName: str | None = Field(default=None, max_length=1024)  # noqa: N815 — ODCS's own field name
    description: str | None = Field(default=None, max_length=4096)
    properties: tuple[Property, ...] = Field(min_length=1)


class Contract(BaseModel):
    """An ODCS v3 data contract, the subset the catalog enforces. Any other field is refused, so none is kept unenforced."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    apiVersion: str = Field(pattern=r"^v3\.\d+\.\d+$")  # noqa: N815 — ODCS's own field name
    kind: Literal["DataContract"]
    id: str = Field(min_length=1, max_length=255)
    version: str = Field(pattern=_SEMVER.pattern)
    status: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    tenant: str | None = Field(default=None, max_length=255)
    domain: str | None = Field(default=None, max_length=255)
    dataProduct: str | None = Field(default=None, max_length=255)  # noqa: N815 — ODCS's own field name
    tags: tuple[str, ...] = ()
    #: `schema` is a method of `BaseModel`, so the field is held under another name and read and written as ODCS spells it.
    table: tuple[SchemaObject, ...] = Field(alias="schema", min_length=1, max_length=1)

    @property
    def major(self) -> int:
        return int(self.version.split(".", 1)[0])

    @property
    def ordered(self) -> tuple[int, int, int]:
        major, minor, patch = (int(part) for part in self.version.split("."))
        return major, minor, patch

    def columns(self) -> dict[str, Property]:
        """Every column the contract names, by its path (``user.email`` for a struct's child)."""
        return dict(_walk(self.table[0].properties, ""))

    def document(self) -> dict[str, Any]:
        """The contract as ODCS spells it."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


def _walk(properties: tuple[Property, ...], prefix: str) -> Iterator[tuple[str, Property]]:
    for prop in properties:
        path = f"{prefix}{prop.name}"
        yield path, prop
        yield from _walk(prop.properties, f"{path}.")


def _unenforced(node: object, path: str) -> Iterator[str]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if key in UNENFORCED_KEYS:
                yield here
            else:
                yield from _unenforced(value, here)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _unenforced(item, f"{path}[{index}]")


def parsed(document: Mapping[str, Any]) -> Contract:
    """The contract ``document`` states, read strictly.

    Raises:
        UnsupportedOperationError: The document states a quality rule or a service-level property, or ``unique`` on a
            column that is no primary key: checks the catalog does not run.
        InvalidInputError: The document is not an ODCS v3 contract of the subset the catalog enforces.
    """
    if promised := sorted(_unenforced(document, "")):
        raise UnsupportedOperationError(
            f"the contract states {', '.join(promised)}: a quality or service-level rule is a check on data or on time, and the catalog "
            "checks a contract's schema on every commit and nothing else. Remove the rule; a pipeline that vets its data tags the version it vetted"
        )
    try:
        contract = Contract.model_validate(document)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()[:8])
        raise InvalidInputError(f"not an ODCS v3 contract of the subset this catalog enforces ({problems})") from exc
    if loose := sorted(path for path, prop in contract.columns().items() if prop.unique and not prop.primaryKey):
        raise UnsupportedOperationError(
            f"the contract states `unique` on {', '.join(loose)}, which is no primaryKey: the catalog refuses a repeated value only of the table's primary key"
        )
    return contract


def _field_at(schema: pa.Schema, path: str) -> pa.Field | None:
    head, *rest = path.split(".")
    if head not in schema.names:
        return None
    field = schema.field(head)
    for name in rest:
        if not pa.types.is_struct(field.type) or field.type.get_field_index(name) < 0:
            return None
        field = field.type.field(name)
    return field


def _in_family(logical: LogicalType, data_type: pa.DataType) -> bool:
    """Whether an Arrow type is one ODCS's ``logical`` type names."""
    types = pa.types
    match logical:
        case "string":
            return types.is_string(data_type) or types.is_large_string(data_type) or types.is_string_view(data_type)
        case "integer":
            return types.is_integer(data_type)
        case "number":
            return types.is_integer(data_type) or types.is_floating(data_type) or types.is_decimal(data_type)
        case "boolean":
            return types.is_boolean(data_type)
        case "date":
            return types.is_date(data_type)
        case "timestamp":
            return types.is_timestamp(data_type)
        case "time":
            return types.is_time(data_type)
        case "object":
            return types.is_struct(data_type) or types.is_map(data_type)
        case "array":
            return types.is_list(data_type) or types.is_large_list(data_type) or types.is_fixed_size_list(data_type)


def violations(contract: Contract, schema: pa.Schema) -> list[str]:
    """What ``schema`` breaks of ``contract``, one sentence per column; empty when it keeps every contracted column."""
    found: list[str] = []
    key = primary_keys.declared_key(schema)
    for path, prop in contract.columns().items():
        field = _field_at(schema, path)
        if field is None:
            found.append(f"column {path!r} is gone (dropped or renamed)")
            continue
        if prop.properties:
            if not pa.types.is_struct(field.type):
                found.append(f"column {path!r} is {field.type}, and the contract states a struct")
        elif str(field.type) != prop.physicalType:
            found.append(f"column {path!r} is {field.type}, and the contract states {prop.physicalType}")
        if prop.logicalType is not None and not _in_family(prop.logicalType, field.type):
            found.append(f"column {path!r} is {field.type}, which is no {prop.logicalType}, the contract's logicalType")
        if prop.required and field.nullable:
            found.append(f"column {path!r} is nullable, and the contract states it required")
        if prop.primaryKey and path not in key:
            found.append(f"column {path!r} is no field of the table's primary key, and the contract states it one")
        elif prop.primaryKey and prop.primaryKeyPosition is not None and key.index(path) + 1 != prop.primaryKeyPosition:
            found.append(f"column {path!r} is at position {key.index(path) + 1} of the table's primary key, and the contract states {prop.primaryKeyPosition}")
    return found


def require_kept(contract: Contract | None, before: pa.Schema | None, after: pa.Schema, *, door: str) -> None:
    """Refuse a commit that breaks the table's ``contract``: one whose schema ``after`` drops, renames, retypes or
    loosens a contracted column that the schema it follows, ``before``, kept. A violation ``before`` already had is not
    this commit's, so it is not refused for it.

    Raises:
        InvalidTableStateError: The commit introduces a violation of the contract.
    """
    if contract is None:
        return
    already = set(violations(contract, before)) if before is not None else set()
    if broken := [violation for violation in violations(contract, after) if violation not in already]:
        raise InvalidTableStateError(
            f"{door}: the commit breaks the table's data contract {contract.id!r} version {contract.version}: {'; '.join(broken)}. "
            "A breaking change is made by setting a contract that allows it, under a higher major version, and committing after"
        )


class ContractRecord(BaseModel):
    """The contract a table holds, and who set it."""

    model_config = ConfigDict(frozen=True)

    table_uuid: str
    document: dict[str, Any]
    set_by: str | None = None
    set_at: datetime

    def contract(self) -> Contract:
        return Contract.model_validate(self.document)


def _key(table_uuid: str) -> str:
    return record_key(control_prefixes.CONTRACTS, "table", table_uuid)


def held(control_root: str, storage_options: StorageOptions, table_uuid: str) -> ContractRecord | None:
    """The contract the table ``table_uuid`` holds, or ``None``. A record that is there and cannot be read refuses: it
    may be the contract a commit breaks.

    Raises:
        ServiceUnavailableError: The store did not answer the read.
        InternalError: The record is there and is not a contract.
    """
    try:
        record = read_record(control_root, storage_options, _key(table_uuid))
        return None if record is None else _checked(ContractRecord.model_validate(record))
    except (OSError, ValueError) as exc:
        raise store_refusal(exc, reading="reading the table's data contract", so="nothing was committed or answered") from exc


def _checked(record: ContractRecord) -> ContractRecord:
    record.contract()
    return record


def _breaks(standing: Contract, asked: Contract) -> list[str]:
    """What ``asked`` no longer promises of what ``standing`` promised."""
    now = asked.columns()
    lost: list[str] = []
    for path, prop in standing.columns().items():
        new = now.get(path)
        if new is None:
            lost.append(f"{path} is no longer named")
        elif (new.physicalType, bool(new.properties)) != (prop.physicalType, bool(prop.properties)):
            lost.append(f"{path} changes type")
        elif (prop.required and not new.required) or (prop.primaryKey and not new.primaryKey):
            lost.append(f"{path} is no longer {'required' if prop.required and not new.required else 'a primaryKey'}")
    return lost


def require_evolves(standing: Contract | None, asked: Contract) -> None:
    """Refuse a contract that takes back a promise of the one standing without a higher major version, or whose version
    does not move forward.

    Raises:
        InvalidInputError: The version went backwards, stayed while the document changed, or a breaking change kept its major.
    """
    if standing is None or standing.document() == asked.document():
        return
    if asked.ordered <= standing.ordered:
        raise InvalidInputError(f"the table holds contract version {standing.version}; a changed contract carries a higher version than {asked.version}")
    if (lost := _breaks(standing, asked)) and asked.major <= standing.major:
        raise InvalidInputError(
            f"the contract takes back what version {standing.version} promises ({'; '.join(lost)}), which is a breaking change: "
            f"give it a major version above {standing.major}"
        )


#: How often a set is tried again when the record it found was deleted under it.
_ROUNDS: Final = 3


def keep(
    control_root: str, storage_options: StorageOptions, table_uuid: str, contract: Contract, *, document: Mapping[str, Any], actor: str | None
) -> ContractRecord:
    """Record ``contract`` as the table's, kept as the ``document`` it was read from so a read answers what was set, over
    the one standing only as :func:`require_evolves` allows. The store arbitrates: the first contract is a put-if-absent,
    a replacement a write over the record it judged.

    Raises:
        InvalidInputError: The contract does not evolve from the one the table holds.
        RecordChangedError: Other sets kept landing first.
    """
    record = ContractRecord(table_uuid=table_uuid, document=dict(document), set_by=actor, set_at=datetime.now(UTC))
    body = record.model_dump(mode="json")

    def _over(stored: dict[str, Any]) -> dict[str, Any]:
        require_evolves(_readable(stored), contract)
        return body

    key = _key(table_uuid)
    for _ in range(_ROUNDS):
        try:
            create_json(control_root, storage_options, key, body)
        except RecordExistsError:
            pass
        else:
            return record
        try:
            mutate_json(control_root, storage_options, key, _over)
        except RecordMissingError:
            continue
        return record
    raise RecordMissingError(key)


def _readable(stored: Mapping[str, Any]) -> Contract | None:
    """The contract a stored record states, or ``None`` for one that is no contract: its owner replaces or deletes such
    a record at the contract's own doors, since every commit is refused while it stands."""
    try:
        return ContractRecord.model_validate(stored).contract()
    except ValidationError:
        return None


def standing(control_root: str, storage_options: StorageOptions, table_uuid: str) -> ContractRecord | None:
    """The record the contract's own doors act on: the one held, or ``None`` for none and for one that cannot be read
    as a contract, which a set replaces and a delete removes.

    Raises:
        ServiceUnavailableError: The store did not answer the read.
    """
    try:
        return held(control_root, storage_options, table_uuid)
    except InternalError:
        return None


def restore(control_root: str, storage_options: StorageOptions, table_uuid: str, record: ContractRecord | None) -> None:
    """Put the table's contract back to ``record``, the one a refused set replaced; none removes it."""
    if record is None:
        forget(control_root, storage_options, table_uuid)
    else:
        put_record(control_root, storage_options, _key(table_uuid), record.model_dump(mode="json"))


def forget(control_root: str, storage_options: StorageOptions, table_uuid: str) -> bool:
    """Delete the table's contract; whether it held one."""
    return delete_record(control_root, storage_options, _key(table_uuid))
