"""Table data endpoints: Arrow-IPC writes, query, count, update/delete, plans, blob serving."""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated

from fastapi import APIRouter, Body, Header, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from lance_namespace import (
    AnalyzeTableQueryPlanRequest,
    CountTableRowsRequest,
    CreateTableResponse,
    DeleteFromTableRequest,
    DeleteFromTableResponse,
    DescribeTableRequest,
    DescribeTableResponse,
    ExplainTableQueryPlanRequest,
    InsertIntoTableRequest,
    InsertIntoTableResponse,
    InvalidInputError,
    MergeInsertIntoTableRequest,
    MergeInsertIntoTableResponse,
    QueryTableRequest,
    UpdateTableRequest,
    UpdateTableResponse,
)

from catalog.api import fga_deps, lineage_deps
from catalog.api.dependencies import (
    ControlEmitterDep,
    FgaClientDep,
    LineageEmitterDep,
    NamespaceDep,
    SettingsDep,
    StorageOptionsDep,
)
from catalog.api.security import CurrentToken
from catalog.core.formats import reject_unsupported_format
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.core.lineage_emit import DELETE, INSERT, MERGE_INSERT, UPDATE, merge_source_pin, parse_run_facets
from catalog.core.serialization import dump
from catalog.schemas import CommitFragmentsRequest, CommitFragmentsResponse
from catalog.services import blob_serving, dataplane, native, table_create
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE


log = logging.getLogger(__name__)


ARROW_FILE = "application/vnd.apache.arrow.file"

router = APIRouter(prefix="/v1/table", tags=["data"])


# The LANCE-ONLY guard now lives in `catalog.core.formats` — four other doors take the same
# `properties` map and none of them called it while it was private to this module.


@router.post("/{id}/create", response_model_exclude_none=True)
async def create_table(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    control: ControlEmitterDep,
    so: StorageOptionsDep,
    data: Annotated[bytes, Body(media_type=ARROW_STREAM_MEDIA_TYPE)],
    mode: str | None = None,
    properties: str | None = None,
    data_base: Annotated[list[str], Query()] = [],  # noqa: B006 — FastAPI Query default, not mutated
    source: str | None = None,
    source_version: Annotated[int | None, Query(ge=1)] = None,
    run_facets_json: Annotated[str | None, Header(alias="X-Lance-Run-Facets")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> CreateTableResponse:
    """Create a Lance table from an Arrow-IPC stream — ``create_table``; seeds ownership + lineage.

    ``properties`` is the spec-0.9 JSON-encoded query parameter. Client-supplied ``storage_options``
    are deliberately NOT accepted: storage access is the catalog's to vend (two-tier secret model),
    so callers can't redirect writes or splice credentials.

    ``data_base`` (#3-B, repeatable) spreads the table's fragments across the named approved buckets (Lance
    multi-base). Each MUST be on the ``LANCE_MULTIBASE_DATA_BASES`` allowlist — a caller can never point a
    base at an arbitrary bucket. Omitted → a single-location table exactly as before.

    Derived-write lineage (S4, optional — the same metadata ``merge_insert`` has always taken):
    ``source`` + ``source_version`` record the version-pinned upstream this table DERIVES FROM
    (an annotation publish from ``transcripts@N``, a Ray job's first write), surfaced as a
    version-pinned INPUT on the CREATE RunEvent; the ``X-Lance-Run-Facets`` header carries producer
    run metadata (e.g. the ``annotationProject`` facet) verbatim onto the same event. Before S4,
    every FIRST write of a derived table was emitted with no pin and no facet — only later merges
    could carry provenance.
    """
    return await table_create.create_governed_table(
        id=id,
        ns=ns,
        settings=settings,
        token=token,
        client=client,
        emitter=emitter,
        control=control,
        so=so,
        data=data,
        mode=mode,
        properties=properties,
        data_base=data_base,
        source=source,
        source_version=source_version,
        run_facets_json=run_facets_json,
        authorization=authorization,
    )


@router.post("/{id}/commit", response_model_exclude_none=True)
async def commit_fragments(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    body: CommitFragmentsRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> CommitFragmentsResponse:
    """Client-DIRECT append commit (#2) — the catalog as the governed commit coordinator.

    The client wrote data fragments straight to object storage with vended, table-scoped creds (never
    through here); this endpoint receives only the tiny serialized ``FragmentMetadata`` + the ``read_version``
    and folds them into a metadata-only Lance ``commit`` under ROOT creds, then emits the INSERT lineage. So
    NO data byte transits the catalog — the byte-proxy's scaling + OOM liability is gone for the bulk-append
    path (the read-modify-write ops — insert/merge_insert/update/delete — and the 2.2-centralizing create
    stay server-side; transaction.md/namespace.md). Writer tier: the router ``authorize`` gate maps
    ``/commit`` to ``can_write_data``. Conflict → 409 (re-read the version + re-commit); schema/version
    mismatch → 400; store outage → 503 (see ``dataplane._classify_commit_error``)."""
    segments = parse_identifier(id, settings.delimiter)
    described: DescribeTableResponse = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=segments))
    if not described.location:
        raise InvalidInputError("table has no object-store location for a client-direct commit")
    version, row_count = await run_in_threadpool(dataplane.commit_appended_fragments, described.location, so, body.fragments, body.read_version, body.run_id)
    # Reuse the shared measured-write emitter (reopens once for version + schema), same as /insert — so the
    # WROTE edge + columnLineage-ready schema land identically whether the append was byte-proxy or direct.
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=INSERT,
        authorization=authorization,
    )
    return CommitFragmentsResponse(version=version, row_count=row_count)


@router.post("/{id}/insert", response_model_exclude_none=True)
async def insert_into_table(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    data: Annotated[bytes, Body(media_type=ARROW_STREAM_MEDIA_TYPE)],
    mode: str | None = None,
    branch: str | None = None,
    authorization: Annotated[str | None, Header()] = None,
) -> InsertIntoTableResponse:
    """Append Arrow-IPC rows — ``insert_into_table``; emits an INSERT lineage event.
    ``branch`` targets a non-main branch (spec 0.9 query param for Arrow-IPC-body ops)."""
    segments = parse_identifier(id, settings.delimiter)
    # Cast the incoming rows to the table's schema first, so a client that infers loose Arrow types (a
    # browser infers float64 for every JS number) can append to int64 columns — else the native append 500s
    # on the mismatch. A genuinely incompatible payload becomes a clean 400 here, not a 500 downstream.
    data = await run_in_threadpool(dataplane.coerce_insert_arrow, ns, so, segments, data)
    req = InsertIntoTableRequest(id=segments, mode=mode, branch=branch)
    response: InsertIntoTableResponse = await run_in_threadpool(dataplane.insert_into_table, ns, so, req, data)
    # Insert's response carries only a transaction_id, not the Lance version it produced — the shared
    # trailer reads version + schema off ONE reopen (best-effort) so the WROTE edge records the real version.
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=INSERT,
        authorization=authorization,
    )
    return response


@router.post("/{id}/merge_insert", response_model_exclude_none=True)
async def merge_insert_into_table(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    client: FgaClientDep,
    data: Annotated[bytes, Body(media_type=ARROW_STREAM_MEDIA_TYPE)],
    on: str | None = None,
    when_matched_update_all: bool | None = None,
    when_matched_update_all_filt: str | None = None,
    when_not_matched_insert_all: bool | None = None,
    when_not_matched_by_source_delete: bool | None = None,
    when_not_matched_by_source_delete_filt: str | None = None,
    timeout: str | None = None,
    use_index: bool | None = None,
    branch: str | None = None,
    source: str | None = None,
    source_version: Annotated[int | None, Query(ge=1)] = None,
    run_facets_json: Annotated[str | None, Header(alias="X-Lance-Run-Facets")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> MergeInsertIntoTableResponse:
    """Upsert Arrow-IPC rows — ``merge_insert_into_table``; emits a MERGE_INSERT lineage event.
    The ``*_filt`` SQL filters, ``timeout``, ``use_index`` and ``branch`` are spec-0.9 query params.

    Training-shaped lineage (optional, catalog stays un-opinionated): ``source`` + ``source_version``
    record the version-pinned upstream this merge DERIVED FROM (a mover's merge from ``source@N`` — the
    reproducibility pin surfaced on the lineage READ edge), and the ``X-Lance-Run-Facets`` header carries
    producer run metadata (e.g. training ``params``) verbatim onto the emitted RunEvent.

    IMPLICIT DDL (§4): after the merge commits, a best-effort BTREE index is ensured on the ``on``
    key (pylance's ``use_index`` only helps *if an index exists*, and nothing else ever builds one —
    without it every upsert full-scans). The FIRST merge on a ``(table, on)`` pays the build
    synchronously; later merges pay one cheap list call. Passing ``use_index=false`` opts out of the
    implicit build too (a caller declining index USAGE shouldn't be charged index CREATION). The
    build commits its OWN Lance version with no lineage event (consistent with
    ``/create_scalar_index`` today), so the first indexed merge leaves the table at
    ``response.version + 1`` while the MERGE_INSERT lineage points at ``response.version`` — a
    version gap, not a lost write."""
    segments = parse_identifier(id, settings.delimiter)
    # Validate the optional lineage metadata BEFORE the write — a malformed pin/facet is a 4xx, not a
    # committed merge whose provenance then silently drops.
    source_pin = merge_source_pin(source, source_version, settings.delimiter)
    extra_run_facets = parse_run_facets(run_facets_json)
    if source_pin is not None:
        # The caller named a source to record as a merge INPUT. The catalog is the TRUSTED stamper on the
        # Dapr transport (the lineage ingest input-authz guard runs only on the HTTP path), so a named source
        # the caller can't READ would forge a cross-tenant DERIVED_FROM/READ edge — or a phantom vertex for a
        # nonexistent one. Mirror the ingest guard here: require can_get_metadata on the source, fail-closed
        # (no-op when FGA is off). A denial 4xx never distinguishes "hidden" from "absent" (both → no tuple).
        await fga_deps.require_can_get_metadata(client, settings, token, segments=source_pin.segments)
    inputs = [source_pin] if source_pin is not None else None
    req = MergeInsertIntoTableRequest(
        id=segments,
        on=on,
        when_matched_update_all=when_matched_update_all,
        when_matched_update_all_filt=when_matched_update_all_filt,
        when_not_matched_insert_all=when_not_matched_insert_all,
        when_not_matched_by_source_delete=when_not_matched_by_source_delete,
        when_not_matched_by_source_delete_filt=when_not_matched_by_source_delete_filt,
        timeout=timeout,
        use_index=use_index,
        branch=branch,
    )
    response: MergeInsertIntoTableResponse = await run_in_threadpool(dataplane.merge_insert_into_table, ns, so, req, data)
    # merge can add/change columns (schema drift at this version) → record the post-write schema, read
    # PINNED at the version this merge produced so a concurrent writer can't smuggle in a later schema.
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=MERGE_INSERT,
        authorization=authorization,
        pin_version=response.version,
        inputs=inputs,
        extra_run_facets=extra_run_facets,
    )
    # AFTER the merge and the emit (matching the emit pattern; §0 forbids BackgroundTasks): ensure the
    # merge key is BTREE-indexed so subsequent upserts stop full-scanning. Best-effort inside — no
    # exception reaches this response (the merge above already committed). use_index=False is the
    # caller's explicit opt-out of index acceleration → also skips the implicit build.
    if use_index is not False:
        await run_in_threadpool(dataplane.ensure_merge_key_index, ns, segments, on, so=so, branch=branch)
    return response


@router.post("/{id}/update", response_model_exclude_none=True)
async def update_table(
    id: str,
    body: UpdateTableRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> UpdateTableResponse:
    """Update rows matching a predicate — ``update_table``; emits an UPDATE lineage event."""
    # LANCE-ONLY (2026-08-15 ruling). An update door is the create door's back way in: a table
    # made as Lance could otherwise be asked to change format afterwards, and the guard's contract is
    # that a format-selecting property is never silently ignored — on any door that accepts one.
    reject_unsupported_format(body.properties)
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    response: UpdateTableResponse = await run_in_threadpool(dataplane.update_table, ns, so, body)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=UPDATE,
        authorization=authorization,
        pin_version=response.version,
    )
    return response


@router.post("/{id}/delete", response_model_exclude_none=True)
async def delete_from_table(
    id: str,
    body: DeleteFromTableRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> DeleteFromTableResponse:
    """Delete rows matching a predicate — ``delete_from_table``; emits a DELETE lineage event."""
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    response: DeleteFromTableResponse = await run_in_threadpool(dataplane.delete_from_table, ns, so, body)
    # A row-delete doesn't change columns, but the WROTE edge at this new version still records the
    # (unchanged) schema so dataset_schema(version=N) is populated for every version, not just writes.
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=DELETE,
        authorization=authorization,
        pin_version=response.version,
    )
    return response


# A single-range ``bytes=`` header: ``bytes=0-3`` / ``bytes=100-`` / ``bytes=-500``. Multi-range and
# other units deliberately don't match — RFC 9110 §14.1.1 lets a server ignore a Range it doesn't
# support, so those fall through to the full 200 rather than a guessy 416.
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_OCTET_STREAM = "application/octet-stream"


def _parse_range(header: str | None) -> tuple[int | None, int | None] | None:
    """Parse a single-range ``Range`` header into ``(first, last)`` (both inclusive; ``None`` = open).

    ``bytes=-n`` (suffix) → ``(None, n)``; ``bytes=a-`` → ``(a, None)``; ``bytes=a-b`` → ``(a, b)``.
    Anything else — malformed, multi-range, non-bytes unit, ``last < first``, the empty ``bytes=-`` —
    returns ``None``, which the endpoint treats as "no range" (full 200), per RFC 9110's permission
    to ignore unsupported Range headers. Pure, so it is unit-testable without a dataset.
    """
    if not header:
        return None
    match = _RANGE_RE.match(header.strip())
    if not match:
        return None
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return None
    if not first:
        return None, int(last)
    if not last:
        return int(first), None
    lo, hi = int(first), int(last)
    return None if hi < lo else (lo, hi)


@router.get("/{id}/blobs")
async def read_table_blob(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    column: Annotated[str, Query(min_length=1)],
    row: Annotated[int, Query(ge=0)],
    version: Annotated[int | None, Query(ge=1)] = None,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    if_range: Annotated[str | None, Header(alias="If-Range")] = None,
) -> Response:
    """Serve one blob payload over plain HTTP — the credential-less consumer path (§9 P1).

    A browser/notebook/service with NO storage credentials fetches blob bytes straight from the
    catalog: ``GET /v1/table/{id}/blobs?column=payload&row=3[&version=N]``. ``Range: bytes=…`` is
    honoured (206 + ``Content-Range``), an unsatisfiable range gets the RFC 416 + ``bytes */size``,
    and every response carries ``Accept-Ranges: bytes`` plus a strong ``ETag``
    (``"<version>-<column>-<row>"``) so clients can resume safely: an ``If-Range`` that no longer
    matches (the table was overwritten mid-download) downgrades the range to a full 200 instead of
    silently splicing bytes from two incarnations. The body is STREAMED in bounded windows (each a
    lazy ``BlobFile.read_range``), so a multi-GB payload never buffers in the catalog — the
    read-side mirror of the write-side body-limit OOM guard. ``row`` is the POSITIONAL index at the
    served version (pin ``version`` for a stable address across overwrites).

    Authz: the router-level ``authorize`` maps the ``blobs`` suffix to reader-tier ``can_read_data``
    (same rung as ``/query``) — this endpoint serves DATA, so credential-vending tiers apply
    unchanged; it just removes the need for the credentials themselves.
    """
    segments = parse_identifier(id, settings.delimiter)
    blob = await run_in_threadpool(
        blob_serving.read_blob,
        ns,
        so,
        segments,
        column=column,
        row=row,
        version=version,
        range_spec=_parse_range(range_header),
        if_range=if_range,
    )
    headers = {"Accept-Ranges": "bytes", "ETag": blob.etag}
    if not blob.satisfiable:
        headers["Content-Range"] = f"bytes */{blob.size}"
        return Response(status_code=416, headers=headers)
    headers["Content-Length"] = str(blob.length)
    if blob.ranged:
        headers["Content-Range"] = f"bytes {blob.start}-{blob.end}/{blob.size}"
    # StreamingResponse iterates the sync generator in a threadpool (starlette iterate_in_threadpool),
    # so each bounded read_range window runs off the event loop and only one window is ever in memory.
    return StreamingResponse(
        blob.chunks(),
        status_code=206 if blob.ranged else 200,
        media_type=_OCTET_STREAM,
        headers=headers,
    )


@router.post("/{id}/query")
def query_table(id: str, body: QueryTableRequest, ns: NamespaceDep, settings: SettingsDep) -> Response:
    """Run a query and return matching rows as an Arrow-IPC file — wraps ``query_table``."""
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    dataplane.refuse_a_branch_this_door_cannot_honour(body.branch, door="query_table")
    data = native.call(ns, "query_table", body)
    return Response(content=data, media_type=ARROW_FILE)


@router.post("/{id}/count_rows")
def count_table_rows(id: str, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep, body: CountTableRowsRequest | None = None) -> Response:
    """Count the table's rows on the ref the request names — ``count_table_rows``; returns plain text.

    Routed through `dataplane` rather than straight to `native.call` so that `branch` is honoured. It
    was not: the parameter reached the upstream implementation, which answered from main regardless,
    so a branch-scoped count returned a plausible number for the wrong dataset with a 200.
    """
    req = body or CountTableRowsRequest()
    req.id = reconcile_body_id(parse_identifier(id, settings.delimiter), req.id)
    return PlainTextResponse(str(dataplane.count_rows(ns, so, req)))


@router.post("/{id}/explain_plan")
def explain_table_query_plan(id: str, body: ExplainTableQueryPlanRequest, ns: NamespaceDep, settings: SettingsDep) -> Response:
    """Return the logical query plan — ``explain_table_query_plan``; plain text."""
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    dataplane.refuse_a_branch_this_door_cannot_honour(body.branch, door="explain_table_query_plan")
    result = native.call(ns, "explain_table_query_plan", body)
    return PlainTextResponse(result if isinstance(result, str) else json.dumps(dump(result)))


@router.post("/{id}/analyze_plan")
def analyze_table_query_plan(id: str, body: AnalyzeTableQueryPlanRequest, ns: NamespaceDep, settings: SettingsDep) -> Response:
    """Return the analyzed query plan with runtime metrics — ``analyze_table_query_plan``; plain text."""
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    dataplane.refuse_a_branch_this_door_cannot_honour(body.branch, door="analyze_table_query_plan")
    result = native.call(ns, "analyze_table_query_plan", body)
    return PlainTextResponse(result if isinstance(result, str) else json.dumps(dump(result)))
