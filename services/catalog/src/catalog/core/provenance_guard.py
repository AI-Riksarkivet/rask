"""What a WRITER may not change about a table's provenance ([[LH-208]]).

Table metadata and column changes are ordinary writer operations in the Lance Namespace spec
(`UpdateTableSchemaMetadata`, `AlterTableAlterColumns`, `AlterTableDropColumns`), so the spec puts no
fence around the keys and columns the estate's provenance rests on. rask therefore reserves them, at
one choke point every door calls, the same shape `core/formats.py` gives the Lance-only rule.

TWO RESERVED NAMESPACES IN SCHEMA METADATA. `lineage.*` holds the #21 self-describing coordinates the
catalog stamps at create and the cascade stamps on each tier; `lineage.dataset_id` is what
maintenance names a medallion dataset by where its path cannot. `rask.*` is the platform's own
namespace. A writer who sets or nulls one of them re-points or erases another party's claim about the
table, so every such write is refused, whichever route the request takes and whatever the create
payload carries.

THE PROVENANCE COLUMNS. A governed tier row is `{id, payload, stage, lineage, source_rowid}`
(`medallion/schemas/tier.py`). `stage`, `lineage` and `source_rowid` are named by
`service_kit.lakehouse.stage_stamp`, and every reader resolves them by that name, so they are protected
by name wherever they appear. The row identity is protected by Lance's own declaration instead: any
field carrying `lance-schema:unenforced-primary-key`, and each of its ancestors, in any table
(`lance_docs/file_format.md` § Unenforced Primary Key). Measured on pylance 12.0.0:

* `drop_columns(["id"])` is accepted and leaves a table with no key;
* `alter_columns` with a `data_type`, even int64 -> int64, is accepted, strips the key metadata and
  re-mints the field id, after which `merge_insert(None)` raises "A merge insert operation requires join
  keys";
* a rename keeps the key but moves the name the cascade merges on (`ingest/runtime.py`, the medallion
  lanes name `id` explicitly);
* dropping or renaming a struct that holds a nested key field is accepted the same way.

Lance itself refuses only `nullable=True` on the key or an ancestor ("Primary key column and all its
ancestors must not be nullable") and any change to the key's field metadata ("the unenforced primary
key is a reserved key and cannot be changed once set"). Those stay Lance's refusals, mapped to 400 by
`dataplane._column_op`, and are not restated here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final

import pyarrow as pa
from lance_namespace import InvalidInputError

from service_kit.lakehouse.stage_stamp import LINEAGE_COLUMN, SOURCE_ROWID_COLUMN, STAGE_COLUMN


#: Schema-metadata namespaces only the platform writes. A key under either is refused at every write door.
RESERVED_METADATA_PREFIXES: Final = ("lineage.", "rask.")

#: Lance's field-metadata key declaring a field part of the unenforced primary key.
PRIMARY_KEY_METADATA: Final = b"lance-schema:unenforced-primary-key"

#: The values Lance reads as "this field is in the key" (`file_format.md`: `true`, `1` or `yes`, case-insensitive).
_PRIMARY_KEY_TRUE: Final = frozenset({b"true", b"1", b"yes"})

#: The tier's provenance columns, by the names `stage_stamp` writes and every reader resolves.
PROVENANCE_COLUMNS: Final = frozenset({STAGE_COLUMN, LINEAGE_COLUMN, SOURCE_ROWID_COLUMN})


def _text(key: str | bytes) -> str:
    return key.decode() if isinstance(key, bytes) else key


def is_reserved_key(key: str | bytes) -> bool:
    """Whether a schema-metadata key belongs to the platform rather than to the table's writer."""
    return _text(key).startswith(RESERVED_METADATA_PREFIXES)


def refuse_reserved_keys(keys: Iterable[str | bytes], *, door: str) -> None:
    """Raise 400 if any of ``keys`` is reserved — a set and a delete alike, since both rewrite the claim.

    Raises:
        InvalidInputError: One or more keys sit under a reserved namespace.
    """
    reserved = sorted({_text(key) for key in keys if is_reserved_key(key)})
    if reserved:
        raise InvalidInputError(
            f"{door}: schema metadata under {', '.join(p + '*' for p in RESERVED_METADATA_PREFIXES)} is written by the platform only; "
            f"refusing to set or delete {', '.join(reserved)}"
        )


def _is_primary_key(field: pa.Field) -> bool:
    value = (field.metadata or {}).get(PRIMARY_KEY_METADATA)
    return value is not None and value.strip().lower() in _PRIMARY_KEY_TRUE


def _key_paths(field: pa.Field, prefix: str = "") -> list[str]:
    """Every primary-key leaf at or under ``field``, as the dotted path Lance's column ops take."""
    path = f"{prefix}{field.name}"
    if _is_primary_key(field):
        return [path]
    if pa.types.is_struct(field.type):
        struct = field.type
        return [p for i in range(struct.num_fields) for p in _key_paths(struct.field(i), f"{path}.")]
    return []


def protected_paths(schema: pa.Schema) -> dict[str, str]:
    """Each column path a writer may not drop, rename or re-type, with why it is protected.

    A key field's ancestors are protected as the key itself is: dropping a struct drops the key inside
    it, and renaming one moves the path a key-less merge resolves.
    """
    protected: dict[str, str] = {name: "a provenance column" for name in schema.names if name in PROVENANCE_COLUMNS}
    for field in schema:
        for key in _key_paths(field):
            protected[key] = "the table's primary key"
            parts = key.split(".")
            for depth in range(1, len(parts)):
                protected.setdefault(".".join(parts[:depth]), f"an ancestor of primary key {key!r}")
    return protected


def _refuse(door: str, verb: str, hits: Mapping[str, str]) -> None:
    named = "; ".join(f"{path!r} is {why}" for path, why in sorted(hits.items()))
    raise InvalidInputError(f"{door}: refusing to {verb} {named} — provenance columns and the primary key are not writer-alterable")


def refuse_provenance_drop(schema: pa.Schema, columns: Sequence[str]) -> None:
    """Raise 400 if dropping ``columns`` would remove a provenance column or any part of the primary key.

    Raises:
        InvalidInputError: A named column is protected, or contains a protected path.
    """
    protected = protected_paths(schema)
    hits = {path: why for path, why in protected.items() for column in columns if path == column or path.startswith(f"{column}.")}
    if hits:
        _refuse("drop_columns", "drop", hits)


def refuse_provenance_alter(schema: pa.Schema, alterations: Sequence[Mapping[str, object]]) -> None:
    """Raise 400 if an alteration renames or re-types a provenance column or any part of the primary key.

    ``alterations`` are in pylance's shape (`path`, `name`, `data_type`, `nullable`). A nullability
    change passes through: Lance refuses it on the key itself, and on a provenance column it rewrites
    nothing a reader resolves.

    Raises:
        InvalidInputError: An alteration renames or re-types a protected path.
    """
    protected = protected_paths(schema)
    hits = {
        str(alteration["path"]): protected[str(alteration["path"])]
        for alteration in alterations
        if str(alteration.get("path")) in protected and ("name" in alteration or "data_type" in alteration)
    }
    if hits:
        _refuse("alter_columns", "rename or re-type", hits)


def _bare(data_type: pa.DataType) -> pa.DataType:
    """``data_type`` with every struct child's metadata and nullability dropped, so two shapes compare by type alone."""
    if pa.types.is_struct(data_type):
        return pa.struct([pa.field(data_type.field(i).name, _bare(data_type.field(i).type)) for i in range(data_type.num_fields)])
    return data_type


def keep_provenance(table_schema: pa.Schema, payload: pa.Table) -> pa.Table:
    """The rows a create ``mode=Overwrite`` writes over an EXISTING table: the payload's, carrying the table's provenance.

    rask serves that Overwrite as a new Lance version of the same table ([[LH-242]]): the id, its grants
    and its time-travel history all continue, so its provenance must continue too. Lance takes an
    overwrite's schema from the payload, metadata included (measured on pylance 12.0.0: a payload with
    no metadata erased ``lineage.*`` and the primary key at top level and inside a struct). So:

    * each top-level column :func:`protected_paths` names (a provenance column, a key field, or an
      ancestor of a nested key) must be in the payload with the table's type, ignoring metadata and
      nullability, and is written as the table's own field, which carries the key back;
    * schema metadata under the reserved namespaces is the table's, whatever the payload stamped; the
      payload's other keys are its own to set.

    Every other column is the payload's to add, drop or re-type, which is what distinguishes this door
    from ``insert?mode=overwrite``, whose rows always take the table's whole schema.

    Raises:
        InvalidInputError: The payload lacks a protected column, or gives one another type.
    """
    protected = {path: why for path, why in protected_paths(table_schema).items() if "." not in path}
    names = set(payload.schema.names)
    hits = {
        path: why
        for path, why in protected.items()
        if path not in names or not _bare(payload.schema.field(path).type).equals(_bare(table_schema.field(path).type))
    }
    if hits:
        _refuse("create?mode=overwrite", "drop or re-type", hits)
    fields = [table_schema.field(field.name) if field.name in protected else field for field in payload.schema]
    columns = [payload.column(field.name).cast(field.type) if field.name in protected else payload.column(field.name) for field in fields]
    metadata = {key: value for key, value in (payload.schema.metadata or {}).items() if not is_reserved_key(key)}
    metadata |= {key: value for key, value in (table_schema.metadata or {}).items() if is_reserved_key(key)}
    return pa.Table.from_arrays(columns, schema=pa.schema(fields, metadata=metadata or None))
