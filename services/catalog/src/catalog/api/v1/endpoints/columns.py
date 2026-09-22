"""Column / schema endpoints (data-plane add/alter/drop + native backfill).

Every op that changes the schema or bumps the Lance version emits a best-effort lineage ``WROTE`` event so
the graph's per-version column inventory follows the evolution (``/datasets/{id}/schema`` + ``/columns``).
The shared ``lineage_deps.emit_measured_write`` trailer reads version + schema off ONE dataset open —
pinned to the response's version when it carries one — and never fails the already-committed mutation.
``backfill_column`` is the one exception: it returns a ``job_id`` (the backfill runs asynchronously), so the
resulting version isn't known synchronously — emitting here would assert a version that hasn't been produced.

The spec's optional ``branch`` on all four data-plane ops is HONORED, not decorative: it selects the ref the
schema evolution commits to, and it rides through to the response's version and the lineage read-back so a
branch write is never reported — or recorded — against main (#100).
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Header
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
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
from openfga_sdk import OpenFgaClient

from catalog.api import fga_deps, lineage_deps
from catalog.api.dependencies import FgaClientDep, LineageEmitterDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.core.lineage_emit import (
    ADD_COLUMNS,
    ALTER_COLUMNS,
    DROP_COLUMNS,
    UPDATE_FIELD_METADATA,
    UPDATE_SCHEMA_METADATA,
)
from catalog.services import dataplane, native
from service_kit.governed import fga
from service_kit.governed.oidc import IDToken


#: The GOVERNANCE namespace in field metadata: keys the estate acts on, not user properties.
#:
#: [[LH-058]]. `rask.classification` decides whether a table's bytes may be vended raw, so whoever can
#: write it can decide that — and this door resolves to `can_write_data` (measured via
#: `_action_relation("table", "update_field_metadata")`). A plain WRITER could therefore clear a
#: classification and make their own table directly vendable again, which is the separation-of-duties
#: hole the reference model's "classify without holding data/DDL rights" names.
#:
#: A PREFIX rather than one key, matching `_INTERNAL_METADATA_PREFIX`'s shape on the table-level twin:
#: the next governance key must not have to remember to add itself to a gate.
GOVERNANCE_FIELD_PREFIX: Final = "rask."


async def _require_classifier_for_governance_keys(
    client: OpenFgaClient | None,
    settings: Settings,
    token: IDToken | None,
    *,
    segments: list[str],
    updates: list[dict[str, Any]],
) -> None:
    """Refuse a governance-key write unless the caller holds ``can_classify`` on the table.

    BODY-AUTHORIZED, like the write-tier check inside `vend_credentials` and `_authorize_grant`: the
    router-wide guard has already cleared `can_write_data` by the time this runs, and the rung this
    needs depends on WHAT the body touches rather than on which route it is.

    A DELETE COUNTS, AND IT HAS THREE SPELLINGS. `update_field_metadata` signals key removal with a
    `None` value — but `UpdateFieldMetadataEntry` carries a third field beside `path` and `metadata`,
    and `dataplane.update_field_metadata` honours it verbatim (`replace = any(bool(u.get("replace")) …)`
    -> `dataset.update_field_metadata(field_updates, replace=replace)`), whose pylance docstring reads
    "completely replace all metadata for the specified fields".

    So `{"path": "payload", "metadata": {}, "replace": true}` names no governance key, drops the
    classification, and leaves the rows untouched — measured: `classified_columns` goes from
    `('payload',)` to `()` with `count_rows()` unchanged. A caller holding only `can_write_data`
    launders the label and keeps the data, which is the entire escape this rung exists to close.

    A REPLACE IS THEREFORE A GOVERNANCE-KEY WRITE BY CONSTRUCTION. Its semantic is "drop every key I did
    not name", and the governance namespace is among them whether or not the body spells it. Gating on
    the body's keys alone reads the one spelling that announces itself and misses the two that do not —
    the same class of miss as reading only non-null values, one field over.
    """
    replaces = any(update.get("replace") for update in updates)
    touched = sorted({key for update in updates for key in (update.get("metadata") or {}) if key.startswith(GOVERNANCE_FIELD_PREFIX)})
    if not touched and not replaces:
        return
    await fga_deps.require_relation(
        client, settings, token, relation="can_classify", obj=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    )


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
        branch=body.branch,
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
        branch=body.branch,
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
        branch=body.branch,
    )
    return response


# The spec's success status, declared on the decorator. All three answer a spec-correct 501 today
# (the `dir` backend stubs them), so nothing exercises the success path — which is exactly why a
# missing `status_code` would go unnoticed until a backend arrived and silently answered 200.
@router.post("/{id}/backfill_column", response_model_exclude_none=True, status_code=202)
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
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> UpdateFieldMetadataResponse:
    """Merge or replace per-field metadata for the given field paths — wraps ``update_field_metadata``;
    emits an UPDATE_FIELD_METADATA event at the new version (columns unchanged, but the WROTE edge keeps
    the per-version schema populated for every version)."""
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    updates = [u.model_dump() for u in (body.updates or [])]
    await _require_classifier_for_governance_keys(client, settings, token, segments=segments, updates=updates)
    response = await run_in_threadpool(dataplane.update_field_metadata, ns, so, segments, updates, body.branch)
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
        branch=body.branch,
    )
    return response


@router.post("/{id}/schema_metadata/update")
async def update_table_schema_metadata(
    id: str,
    body: dict[str, Any],
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Upsert the table's schema-level metadata map — a ``null`` value DELETES that key; emits an
    UPDATE_SCHEMA_METADATA event (the response omits the version, so it is read back best-effort).

    The op MERGES, whatever the spec's "Replace" wording says (probed against a real ``dir`` backend:
    posting ``{owner}`` over ``{owner, tier}`` leaves ``tier`` standing). Omitting a key therefore cannot
    remove it, and the spec's request model types ``metadata`` as a strict ``{str: str}`` that cannot carry
    a null — so ``{"key": null}`` is a rask EXTENSION, routed to the dataplane's pylance
    ``update_schema_metadata`` (the same ``None``-deletes dialect ``update_field_metadata`` already speaks).
    A body with no nulls stays on the native spec op, transaction id and all.
    """
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
        # THE BRANCH RIDES THE ENVELOPE TOO, and dropping it rewrote MAIN's properties while answering
        # 200. The spec's own `branch` parameter description says where it lives: the query form is
        # "used by branch-scoped operations that cannot carry a `branch` field in their request body
        # ... Operations with a JSON request body carry `branch` as a body field instead." This route
        # has a JSON body, so the envelope is where it belongs — and it was reading `id` from there
        # while ignoring `branch` in the same dict.
        raw_branch = body.get("branch")
        branch = raw_branch if isinstance(raw_branch, str) and raw_branch else None
        raw = nested
    else:
        # A FLAT body IS the metadata map, so there is no envelope to read a branch out of — the spec's
        # query parameter is the only channel left, and this route does not serve one. Stated rather
        # than left implicit: main is the correct target here, not an oversight.
        branch = None
        raw = body
    # `str(v)` on a null would write the literal string "None" — the deletion signal must survive intact.
    values: dict[str, str | None] = {str(k): None if v is None else str(v) for k, v in raw.items()}
    response: UpdateTableSchemaMetadataResponse
    # A BRANCH TAKES THE DATAPLANE PATH WHICHEVER DIALECT THE BODY SPEAKS. Passing `branch` to the
    # native op is not enough and looked like it was: the spec request carries the field, the catalog
    # filled it in, and the upstream implementation disregards it — so a null-free body on a branch
    # rewrote MAIN's properties and answered 200, while the same call carrying a null was isolated
    # correctly. Verified live 2026-08-31 against the object store: main advanced a version and took
    # the branch's key. The dataplane implementation merges and null-deletes with the same semantics
    # and opens the ref it is given, so routing a branch through it is the whole fix rather than a
    # detour around one. It costs the native op's transaction id on branch writes, which is a real
    # trade and the right way round: an id naming a commit to the wrong dataset is worth less than no
    # id at all.
    if branch is not None or any(v is None for v in values.values()):
        updated = await run_in_threadpool(dataplane.update_schema_metadata, ns, so, segments, values, branch=branch)
        response = UpdateTableSchemaMetadataResponse(metadata=updated)
    else:
        req = UpdateTableSchemaMetadataRequest(id=segments, metadata={k: v for k, v in values.items() if v is not None}, branch=branch)
        response = await run_in_threadpool(native.call, ns, "update_table_schema_metadata", req)
        # ONE DOOR, ONE ANSWER. The dataplane route above already hides the catalog's own `lineage.*`
        # keys; the native op returns them verbatim, so without this the same door described a table
        # two different ways depending on which route the request happened to take — and the map a
        # caller sees is the map it saves back.
        response.metadata = dataplane.filter_internal_metadata(response.metadata or {})
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
    # THE DIRECT MAP, per the spec's REST-only rule: the body IS the updated metadata, not an envelope
    # around it. rask answered `{metadata, transaction_id}`, on which pylance's Rust client raises
    # `invalid type: map, expected a string` — AFTER the write has committed, so a spec client sees a
    # failure for a mutation that happened. `transaction_id` and the null-deletes dialect are rask
    # extensions and belong on the management API (R2), not on a spec route whose response a stock
    # client must be able to deserialise.
    return JSONResponse(content=response.metadata or {})
