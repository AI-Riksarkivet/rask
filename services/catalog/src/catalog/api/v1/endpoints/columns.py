"""Column / schema endpoints (data-plane add/alter/drop + native backfill).

Every op that changes the schema or bumps the Lance version emits a best-effort lineage ``WROTE`` event so
the graph's per-version column inventory follows the evolution (``/datasets/{id}/schema`` + ``/columns``).
The shared ``lineage_deps.emit_measured_write`` trailer reads version + schema off ONE dataset open —
pinned to the response's version when it carries one — and never fails the already-committed mutation.
``backfill_column`` is the one exception: it returns a ``job_id`` (the backfill runs asynchronously), so the
resulting version isn't known synchronously — emitting here would assert a version that hasn't been produced.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    AlterTableAddColumnsRequest,
    AlterTableAddColumnsResponse,
    AlterTableAlterColumnsRequest,
    AlterTableAlterColumnsResponse,
    AlterTableBackfillColumnsRequest,
    AlterTableBackfillColumnsResponse,
    AlterTableDropColumnsRequest,
    AlterTableDropColumnsResponse,
    UpdateFieldMetadataRequest,
    UpdateFieldMetadataResponse,
    UpdateTableSchemaMetadataRequest,
    UpdateTableSchemaMetadataResponse,
)

from catalog.api import lineage_deps
from catalog.api.dependencies import LineageEmitterDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.core.lineage_emit import (
    ADD_COLUMNS,
    ALTER_COLUMNS,
    DROP_COLUMNS,
    UPDATE_FIELD_METADATA,
    UPDATE_SCHEMA_METADATA,
)
from catalog.services import dataplane, native


router = APIRouter(prefix="/v1/table", tags=["columns"])


@router.post("/{id}/add_columns", response_model_exclude_none=True)
async def add_columns(
    id: str,
    body: AlterTableAddColumnsRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> AlterTableAddColumnsResponse:
    """Add SQL-expression-computed columns to the table — wraps ``alter_table_add_columns``; emits an
    ADD_COLUMNS event carrying the NEW per-version schema so the graph's column inventory follows the add."""
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    response = await run_in_threadpool(dataplane.add_columns, ns, so, body)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=ADD_COLUMNS,
        authorization=authorization,
        pin_version=response.version,
    )
    return response


@router.post("/{id}/alter_columns", response_model_exclude_none=True)
async def alter_columns(
    id: str,
    body: AlterTableAlterColumnsRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> AlterTableAlterColumnsResponse:
    """Rename, re-type, or change nullability of existing columns — wraps ``alter_table_alter_columns``;
    emits an ALTER_COLUMNS event with the post-evolution schema (renames/re-types show in the graph)."""
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    response = await run_in_threadpool(dataplane.alter_columns, ns, so, body)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=ALTER_COLUMNS,
        authorization=authorization,
        pin_version=response.version,
    )
    return response


@router.post("/{id}/drop_columns", response_model_exclude_none=True)
async def drop_columns(
    id: str,
    body: AlterTableDropColumnsRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> AlterTableDropColumnsResponse:
    """Drop the named columns from the table — wraps ``alter_table_drop_columns``; emits a DROP_COLUMNS
    event with the reduced schema so the dropped columns leave the graph's per-version inventory."""
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    response = await run_in_threadpool(dataplane.drop_columns, ns, so, body)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=DROP_COLUMNS,
        authorization=authorization,
        pin_version=response.version,
    )
    return response


@router.post("/{id}/backfill_column", response_model_exclude_none=True)
def backfill_column(id: str, body: AlterTableBackfillColumnsRequest, ns: NamespaceDep, settings: SettingsDep) -> AlterTableBackfillColumnsResponse:
    """Backfill values into columns via the native driver — wraps ``alter_table_backfill_columns``.

    No lineage is emitted here: the response carries a ``job_id`` (the backfill runs asynchronously), so the
    Lance version it eventually produces isn't known at request time — a synchronous emit would assert a
    version that hasn't been written. The version bump is recovered by #23 reconcile when the job lands.
    """
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    return native.call(ns, "alter_table_backfill_columns", body)


@router.post("/{id}/update_field_metadata", response_model_exclude_none=True)
async def update_field_metadata(
    id: str,
    body: UpdateFieldMetadataRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> UpdateFieldMetadataResponse:
    """Merge or replace per-field metadata for the given field paths — wraps ``update_field_metadata``;
    emits an UPDATE_FIELD_METADATA event at the new version (columns unchanged, but the WROTE edge keeps
    the per-version schema populated for every version)."""
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    updates = [u.model_dump() for u in (body.updates or [])]
    response = await run_in_threadpool(dataplane.update_field_metadata, ns, so, segments, updates)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=UPDATE_FIELD_METADATA,
        authorization=authorization,
        pin_version=response.version,
    )
    return response


@router.post("/{id}/schema_metadata/update", response_model_exclude_none=True)
async def update_table_schema_metadata(
    id: str,
    body: dict[str, Any],
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> UpdateTableSchemaMetadataResponse:
    """Set the table's schema-level metadata map — wraps ``update_table_schema_metadata``; emits an
    UPDATE_SCHEMA_METADATA event (the response omits the version, so it is read back best-effort)."""
    # REST-only: the spec sends the metadata map directly, or wrapped as {"metadata": {...}}.
    segments = parse_identifier(id, settings.delimiter)
    nested = body.get("metadata")
    if isinstance(nested, dict):
        # Spec envelope: the id (+ identity/context) sits BESIDE the map — reconcile it like every {id}
        # route (differing → 400). Only the envelope form is inspected: a flat body IS the metadata map,
        # so keys literally named "id"/"identity"/"context" in it are user data, never envelope fields
        # (audit 2026-07-15 — the first cut popped them from the flat form and silently dropped them).
        raw_id = body.get("id")
        reconcile_body_id(segments, raw_id if isinstance(raw_id, list) else None)
        raw = nested
    else:
        raw = body
    metadata: dict[str, str] = {str(k): str(v) for k, v in raw.items()}
    req = UpdateTableSchemaMetadataRequest(id=segments, metadata=metadata)
    response: UpdateTableSchemaMetadataResponse = await run_in_threadpool(native.call, ns, "update_table_schema_metadata", req)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=UPDATE_SCHEMA_METADATA,
        authorization=authorization,
    )
    return response
