"""A table's columns and metadata: add, alter and drop columns, and its schema-level and field metadata."""

from collections import Counter
from collections.abc import Iterable
from typing import Any, cast

import lance
import pyarrow as pa
from lance.dataset import AlterColumn  # noqa: TID251 — a type lance exports only from its dataset module, not an open
from lance_namespace import (
    AlterTableAddColumnsRequest,
    AlterTableAddColumnsResponse,
    AlterTableAlterColumnsRequest,
    AlterTableAlterColumnsResponse,
    AlterTableDropColumnsRequest,
    AlterTableDropColumnsResponse,
    InvalidInputError,
    UnsupportedOperationError,
    UpdateFieldMetadataResponse,
)

from catalog.core import json_arrow, provenance_guard
from catalog.core.lance_refusals import column_op
from catalog.core.version_retention import recorded_branch
from catalog.services import table_claims
from catalog.services.tables.commits import captured, commit_stamped, commit_stamped_keeping_labels
from lakehouse.storage.options import StorageOptions


__all__ = [
    "add_columns",
    "alter_columns",
    "drop_columns",
    "filter_internal_metadata",
    "update_field_metadata",
    "update_schema_metadata",
    "user_schema_metadata",
]


def add_columns(
    table: table_claims.DoorTable, so: StorageOptions, req: AlterTableAddColumnsRequest, *, transaction_properties: dict[str, str]
) -> AlterTableAddColumnsResponse:
    """Add columns computed from per-column SQL expressions, committed with ``transaction_properties`` ([[LH-497]]).

    Every entry is added as it declares, or the request is refused ([[LH-537]]). ``spec.yaml`` AddColumnsEntry declares a
    column three ways: an ``expression`` evaluated once over the existing rows, a ``virtual_column`` a UDF computes, and a
    ``computed`` column added all-null with its expression kept as the binding a backfill fills. The ``dir`` backend has
    no UDF runtime, no binding and no backfill, so an entry declaring either of the last two is refused as the spec's 406
    whatever else it declares, rather than added from its expression as a column it did not ask for.

    Raises:
        UnsupportedOperationError: An entry declares a ``virtual_column`` or a ``computed`` column.
        InvalidInputError: There is no entry, an entry declares no expression, or two entries name one column.
    """
    columns = req.new_columns or []
    if any(column.virtual_column is not None for column in columns):
        raise UnsupportedOperationError("virtual_column add_columns is not supported by this backend")
    if any(column.computed is not None for column in columns):
        raise UnsupportedOperationError(
            "computed add_columns is not supported by this backend — the expression binding and its backfill are both unimplemented for the dir backend"
        )
    if not columns or any(not column.expression for column in columns):
        raise InvalidInputError("add_columns requires a SQL expression for each new column")
    named = Counter(column.name for column in columns)
    if twice := sorted(name for name, count in named.items() if count > 1):
        raise InvalidInputError(f"add_columns names each new column once, but names {twice} more than once")
    transforms = {column.name: column.expression for column in columns if column.expression}
    dataset = table.open(so, branch=req.branch)
    with column_op("add_columns", dataset.schema.names):
        version, _ = commit_stamped(
            table,
            so,
            transaction_properties,
            plan=captured(table, so, lambda handle: handle.add_columns(transforms), branch=req.branch),
            branch=req.branch,
            opened=dataset,
        )
    return AlterTableAddColumnsResponse(version=version)


def alter_columns(
    table: table_claims.DoorTable, so: StorageOptions, req: AlterTableAlterColumnsRequest, *, transaction_properties: dict[str, str]
) -> AlterTableAlterColumnsResponse:
    """Rename / re-type / change nullability of existing columns, committed with ``transaction_properties`` ([[LH-497]]).

    A ``virtual_column`` alteration (a UDF-backed column) is declined as Unsupported (406, code 0), as ``add_columns``
    declines one: pylance's ``alter_columns`` takes a name, a type and a nullability and nothing that carries a UDF.
    """
    if any(entry.virtual_column is not None for entry in req.alterations or []):
        raise UnsupportedOperationError("virtual_column alter_columns is not supported by this backend")
    alterations: list[dict[str, object]] = []
    for entry in req.alterations or []:
        alteration: dict[str, object] = {"path": entry.path}
        if entry.rename is not None:
            alteration["name"] = entry.rename
        if entry.nullable is not None:
            alteration["nullable"] = entry.nullable
        dt = getattr(entry, "data_type", None)
        if dt is not None:
            # data_type is the spec's JSON form of a type, read as the describe door writes it; pylance needs a pa.DataType.
            alteration["data_type"] = json_arrow.arrow_type(dt if isinstance(dt, dict) else dt.model_dump())
        alterations.append(alteration)
    dataset = table.open(so, branch=req.branch)
    schema = dataset.schema
    provenance_guard.refuse_provenance_alter(schema, alterations)

    def alter(handle: lance.LanceDataset) -> None:
        # Each positional argument is one alteration dict at runtime (pylance 13.0.0 passes `list(alterations)` on); its
        # stub types each as `Iterable[AlterColumn]`.
        handle.alter_columns(*cast(list[Iterable[AlterColumn]], alterations))

    with column_op("alter_columns", schema.names):
        version, _ = commit_stamped_keeping_labels(
            table, so, transaction_properties, plan=captured(table, so, alter, branch=req.branch), branch=req.branch, opened=dataset
        )
    return AlterTableAlterColumnsResponse(version=version)


def drop_columns(
    table: table_claims.DoorTable, so: StorageOptions, req: AlterTableDropColumnsRequest, *, transaction_properties: dict[str, str]
) -> AlterTableDropColumnsResponse:
    """Drop the named columns from the table, committed with ``transaction_properties`` ([[LH-497]])."""
    columns = list(req.columns or [])
    dataset = table.open(so, branch=req.branch)
    provenance_guard.refuse_provenance_drop(dataset.schema, columns)
    with column_op("drop_columns", dataset.schema.names):
        version, _ = commit_stamped_keeping_labels(
            table,
            so,
            transaction_properties,
            plan=captured(table, so, lambda handle: handle.drop_columns(columns), branch=req.branch),
            branch=req.branch,
            opened=dataset,
        )
    return AlterTableDropColumnsResponse(version=version)


def filter_internal_metadata(metadata: dict[str, str]) -> dict[str, str]:
    """Drop the platform's reserved keys (`provenance_guard.RESERVED_METADATA_PREFIXES`) from a map a caller will see.

    They are the coordinates that make the Lance file self-describing, not user properties. The map a
    caller holds is the map it saves back, and every write door refuses a reserved key, so handing them
    out would hand the caller a map it cannot save.
    """
    return {k: v for k, v in metadata.items() if not provenance_guard.is_reserved_key(k)}


def user_schema_metadata(schema: pa.Schema) -> dict[str, str]:
    """``schema``'s schema-level metadata as ``{str: str}``, without the reserved ``lineage.*`` / ``rask.*`` keys."""
    out: dict[str, str] = {}
    for k, v in (schema.metadata or {}).items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        out[key] = v.decode() if isinstance(v, bytes) else str(v)
    return filter_internal_metadata(out)


def update_schema_metadata(
    table: table_claims.DoorTable,
    so: StorageOptions,
    values: dict[str, str | None],
    *,
    branch: str | None = None,
    transaction_properties: dict[str, str],
) -> tuple[dict[str, str], int]:
    """Upsert the table's schema-level metadata; a ``None`` value DELETES that key.

    The table-level twin of :func:`update_field_metadata`'s dialect, and the only way to REMOVE a table
    property: the spec's ``update_table_schema_metadata`` merges (probed against a real ``dir`` backend —
    ``{owner}`` over ``{owner, tier}`` leaves ``tier`` standing, despite the spec text saying "Replace"),
    and its wire model types ``metadata`` as a strict ``{str: str}``, so a null cannot ride the native op
    at all. Omitting a key is therefore a no-op, which made the properties editor's remove button a silent
    lie until this existed.

    NEVER ``replace=True``. The map a caller holds came from :func:`read_schema_metadata`, which EXCLUDES
    the reserved ``lineage.*`` / ``rask.*`` keys — so replacing would silently drop the #21 coordinates that make
    the Lance file self-describing. Merge + explicit null-delete is the only shape that can't destroy them.
    ``values`` naming a reserved key is the DOOR's to refuse (``provenance_guard.refuse_reserved_keys``):
    this function is also how the platform itself writes them.

    Returns the table's new full map with the reserved keys filtered out, matching what the read twin
    reports, read off the version the update committed, and that version, so the lineage event names this
    commit rather than whatever a reopen finds. Committed with ``transaction_properties`` ([[LH-497]]).
    """
    version, _ = commit_stamped(
        table, so, transaction_properties, plan=captured(table, so, lambda handle: handle.update_schema_metadata(values), branch=branch), branch=branch
    )
    committed = table.open_unchecked(so, version=version, branch=recorded_branch(branch))
    return user_schema_metadata(committed.schema), version


def update_field_metadata(
    table: table_claims.DoorTable,
    so: StorageOptions,
    updates: list[dict[str, Any]],
    *,
    branch: str | None = None,
    transaction_properties: dict[str, str],
) -> UpdateFieldMetadataResponse:
    """Merge or replace each entry's field metadata, committed as one version with ``transaction_properties`` ([[LH-497]]).

    ``replace`` belongs to its entry (``lance_docs/ns_catalog/spec.yaml`` UpdateFieldMetadataEntry), so a request mixing
    the two replaces only the fields whose entries ask (:func:`_apply_field_metadata`). The last entry naming a path
    decides that path.

    Args:
        table: The table the door resolved.
        so: The storage options the door opens it with.
        updates: The request's entries, each a ``path``, its ``metadata`` (a ``None`` value deletes a key) and ``replace``.
        branch: The ref to commit on; ``None`` is main.
        transaction_properties: The stamp the commit carries.

    Returns:
        The version committed and each path's metadata as the request set it.

    Raises:
        TableColumnNotFoundError: An entry's path names no field (code 12).
        ConcurrentModificationError: Another commit took the version this one planned on (code 14).
        InvalidInputError: Lance refuses the update as the caller's mistake (code 13).
    """
    entries = {u["path"]: u for u in updates if u.get("path")}
    field_updates = {path: dict(entry.get("metadata") or {}) for path, entry in entries.items()}
    replacing = {path for path, entry in entries.items() if entry.get("replace")}
    dataset = table.open(so, branch=branch)
    with column_op("update_field_metadata", dataset.schema.names):
        version, _ = commit_stamped(
            table,
            so,
            transaction_properties,
            plan=captured(table, so, lambda handle: _apply_field_metadata(handle, field_updates, replacing=replacing), branch=branch),
            branch=branch,
            opened=dataset,
        )
    # A None value is the key-deletion signal for the backend; drop those from the
    # echoed map since the response model's field values are non-nullable strings.
    fields = {path: {k: v for k, v in meta.items() if v is not None} for path, meta in field_updates.items()}
    return UpdateFieldMetadataResponse(version=version, fields=fields)


def _apply_field_metadata(handle: lance.LanceDataset, field_updates: dict[str, dict[str, str | None]], *, replacing: set[str]) -> None:
    """Apply ``field_updates`` on ``handle`` in ONE Lance call, so the request commits one version.

    Lance's ``update_field_metadata`` takes one ``replace`` for the whole call. With no path in ``replacing`` every entry
    merges. Otherwise the call replaces, and each merging entry names its field's whole metadata as ``handle``, the
    version the call is planned on, holds it, with the entry's keys applied (:func:`_merged_field_metadata`).
    """
    if not replacing:
        handle.update_field_metadata(field_updates)
        return
    merged = {path: _merged_field_metadata(handle, path, metadata) for path, metadata in field_updates.items() if path not in replacing}
    handle.update_field_metadata({**field_updates, **merged}, replace=True)


def _merged_field_metadata(handle: lance.LanceDataset, path: str, metadata: dict[str, str | None]) -> dict[str, str | None]:
    """The metadata of the field at ``path`` on ``handle`` with ``metadata`` merged in.

    A ``None`` value stays in, and the replacing call drops its key as a merging one does (measured on pylance 13.0.0). A
    path no field resolves is answered as given, for Lance's own call to refuse (code 12, :func:`column_op`).

    Raises:
        TypeError: pylance answers the field's metadata as something other than a dict.
    """
    field = handle.lance_schema.field(path)
    if field is None:
        return metadata
    # pylance 13.0.0 serves `LanceField.metadata` as a dict of strings, an attribute its stub does not declare.
    held = getattr(field, "metadata", None)
    if not isinstance(held, dict):
        raise TypeError(f"pylance answered the metadata of field {path!r} as {type(held).__name__}, not a dict")
    return {**held, **metadata}
