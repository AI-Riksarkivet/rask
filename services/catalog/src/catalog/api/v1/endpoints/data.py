"""Table data endpoints: Arrow-IPC writes, query, count, update/delete, plans, blob serving."""

from __future__ import annotations

import json
import logging
import re
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Body, Header, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from lance_namespace import (
    AnalyzeTableQueryPlanRequest,
    CountTableRowsRequest,
    CreateTableResponse,
    DeleteFromTableRequest,
    DeleteFromTableResponse,
    ExplainTableQueryPlanRequest,
    InsertIntoTableRequest,
    InsertIntoTableResponse,
    MergeInsertIntoTableRequest,
    MergeInsertIntoTableResponse,
    QueryTableRequest,
    QueryTableResponse,
    UpdateTableRequest,
    UpdateTableResponse,
)

from catalog.api import fga_deps, lineage_deps
from catalog.api import idempotency as idem
from catalog.api.dependencies import (
    ControlEmitterDep,
    DataNamespaceDep,
    DoorTableDep,
    FgaClientDep,
    LineageEmitterDep,
    NamespaceDep,
    NamespaceRootDep,
    ObjectUuidsDep,
    ResolverDep,
    RunDep,
    SettingsDep,
    StorageOptionsDep,
)
from catalog.api.rask_params import RaskDataBase, RaskExternalBlobBase, RaskFlag
from catalog.api.security import Caller, CurrentToken
from catalog.core import provenance_guard, run_context
from catalog.core.formats import reject_unsupported_properties
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.core.lineage_emit import DELETE, INSERT, MERGE_INSERT, OVERWRITE_TABLE, UPDATE
from catalog.core.modes import CreateMode, InsertMode
from catalog.core.run_context import RunContext
from catalog.core.serialization import dump
from catalog.schemas import TableChangesRequest
from catalog.services import blob_serving, dataplane, table_create
from service_kit.governed.audit import audit_read
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE


log = logging.getLogger(__name__)


ARROW_FILE = "application/vnd.apache.arrow.file"

_OCTET_STREAM = "application/octet-stream"


class ArrowFileResponse(Response):
    """Declares Arrow FILE framing to the SCHEMA generator; the handler still builds its own response.

    FastAPI takes a door's documented 200 content type from `response_class`, and defaults it to
    `JSONResponse` — so without this, `docs/catalog-openapi.json` advertises `application/json` for a
    door that has never answered JSON, and `frontend/packages/api/src/generated/catalog.ts` is
    generated from that document ([[LH-186]]). Passing `responses=` alone does NOT fix it: FastAPI
    merges the two and the JSON default stays alongside the truth.
    """

    media_type = ARROW_FILE


class OctetStreamResponse(Response):
    """The same declaration for the blob door, whose payload is arbitrary bytes."""

    media_type = _OCTET_STREAM


router = APIRouter(prefix="/v1/table", tags=["data"])

#: THE MANAGEMENT SURFACE ([[LH-021]]). This module serves SPEC operations on `router` and rask's own
#: on this one — split by AUDIENCE rather than by topic, because a spec client discovering a verb like
#: `blobs` or `commit` on a spec prefix meets something the Lance namespace document never defines.
#: Both routers inherit the same three rungs from `api/v1/router.py`.
management_router = APIRouter(prefix="/management/v1/table", tags=["data-management"])


# The LANCE-ONLY guard now lives in `catalog.core.formats` — four other doors take the same
# `properties` map and none of them called it while it was private to this module.


def _stamp(run: RunContext | None, token: Caller | None) -> dict[str, str] | None:
    """The transaction properties a write door stamps: the request's run, committed by the verified caller."""
    return run_context.commit_properties(run, author=token.subject if token is not None else None)


@router.post("/{id}/create", response_model_exclude_none=True)
async def create_table(
    id: str,
    ns: NamespaceDep,
    root: NamespaceRootDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    control: ControlEmitterDep,
    so: StorageOptionsDep,
    run: RunDep,
    resolver: ResolverDep,
    data: Annotated[bytes, Body(media_type=ARROW_STREAM_MEDIA_TYPE)],
    mode: str | None = None,
    properties: str | None = None,
    data_base: RaskDataBase = [],  # noqa: B006 — FastAPI Query default, not mutated
    external_blob_base: RaskExternalBlobBase = None,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: idem.IdempotencyKeyHeader = None,
    force: RaskFlag = False,
) -> CreateTableResponse:
    """Create a Lance table from an Arrow-IPC stream — ``create_table``; seeds ownership + lineage.

    ``mode=Overwrite`` over an existing table writes a new version of it ([[LH-242]],
    ``table_create``'s module docstring): a protected table refuses it 409 unless ``force=true``, which
    turns the protection lock only, as on drop.

    ``properties`` is the spec-0.9 JSON-encoded query parameter. Client-supplied ``storage_options``
    are deliberately NOT accepted: storage access is the catalog's to vend (two-tier secret model),
    so callers can't redirect writes or splice credentials.

    ``data_base`` (#3-B, repeatable) spreads the table's fragments across the named approved buckets (Lance
    multi-base). Each MUST be on the ``LANCE_MULTIBASE_DATA_BASES`` allowlist — a caller can never point a
    base at an arbitrary bucket. Omitted → a single-location table exactly as before.

    ``external_blob_base`` ([[LH-209]]) asks to register ONE external blob base, so the table's blob
    columns may hold ``Blob.from_uri`` pointers under it. It must lie inside a ``LANCE_EXTERNAL_BLOB_BASES``
    entry and outside governed storage (``table_bases.requested_external_blob_base``). Omitted → no
    external base, and a pointer in the rows is refused.

    The run the request's Context names (:mod:`catalog.core.run_context`) is stamped into the commit and its inputs
    become the create event's version-pinned inputs; the tier judge has already refused a silver or gold create that
    names none, and an input the caller cannot read.
    """
    # BEFORE `idem.begin`, like the register door's: a malformed mode, id, base or property map is a SHAPE
    # refusal, and a claim minted for it would hold the key for its lease and answer the corrected retry 409.
    create_mode = CreateMode.parse(mode)
    shape = table_create.parse_create_shape(
        id,
        settings=settings,
        data_base=data_base,
        external_blob_base=external_blob_base,
        properties=properties,
    )
    # The body is shape too, read off the loop. The table read here is the one written, so the payload
    # is decoded once.
    table = await run_in_threadpool(dataplane.read_arrow_body, data, max_bytes=settings.max_body_bytes)
    # The payload's schema metadata becomes the table's, so a reserved key in it is a forged claim
    # whether or not the lineage stamp would later overwrite it ([[LH-208]]).
    provenance_guard.refuse_reserved_keys((table.schema.metadata or {}).keys(), door="create payload")
    # OPTIONAL BY SPEC CONSTRAINT (see `catalog.api.idempotency`): a stock Lance client sends no key
    # and is unaffected. A caller that sends one gets the first attempt's answer back rather than a
    # second execution of a door that DROPS AND REWRITES the dataset under `mode=Overwrite`.
    converge = await idem.begin(settings, so, token, idempotency_key, endpoint="POST /v1/table/{id}/create")
    if converge.replay is not None:
        return CreateTableResponse.model_validate(converge.replay.body)

    response = await table_create.create_governed_table(
        id=id,
        ns=ns,
        root=root,
        settings=settings,
        token=token,
        client=client,
        emitter=emitter,
        uuids=uuids,
        control=control,
        resolver=resolver,
        so=so,
        table=table,
        mode=create_mode,
        shape=shape,
        data_base=data_base,
        authorization=authorization,
        run=run,
        force=force,
    )
    # AFTER the door succeeded, and only then: a failure raises past this line, leaving the claim
    # in-flight until its lease expires rather than recording an outcome the caller never received.
    await converge.remember(200, response)
    return response


# The published contract requires a body; the handler still accepts none, so a missing body meets the
# same 400 `InvalidInput` as missing bounds instead of FastAPI's 422.
@router.post("/{id}/insert", response_model_exclude_none=True)
async def insert_into_table(
    id: str,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    run: RunDep,
    data: Annotated[bytes, Body(media_type=ARROW_STREAM_MEDIA_TYPE)],
    mode: str | None = None,
    branch: Annotated[str | None, Query(description="The ref to insert into. Omit for main.")] = None,
    authorization: Annotated[str | None, Header()] = None,
    force: RaskFlag = False,
) -> InsertIntoTableResponse:
    """Append Arrow-IPC rows — ``insert_into_table``; emits an INSERT lineage event.
    ``branch`` targets a non-main branch (spec 0.9 query param for Arrow-IPC-body ops).

    ``mode=overwrite`` removes every row on the ref before inserting (spec.yaml InsertIntoTableRequest.mode),
    so a protected table refuses it 409 unless ``force=true``, on any branch — the protection record is the
    table's — and it is recorded as ``overwrite_table`` with the ``OVERWRITE`` lifecycle state ([[LH-242]]).

    The run the request's Context names is stamped into the commit, on main only ([[LH-225]])."""
    segments = parse_identifier(id)
    # PARSED HERE, ONCE: pylance's `LanceDataset.insert` refuses a value outside Append/Overwrite with a
    # bare ValueError or OSError (500, measured on pylance 12.0.0), where the spec answers InvalidInput.
    # Ahead of the coerce because that opens the dataset, and a shape refusal costs no round trip.
    insert_mode = InsertMode.parse(mode)
    if insert_mode is InsertMode.OVERWRITE:
        await fga_deps.require_table_unprotected(settings, table.resolved, force=force)
    # Cast the incoming rows to the table's schema first, so a client that infers loose Arrow types (a
    # browser infers float64 for every JS number) can append to int64 columns — else the append 500s on
    # the mismatch. A genuinely incompatible payload becomes a clean 400 here, not a 500 downstream.
    data = await run_in_threadpool(
        dataplane.coerce_insert_arrow, table, so, data, branch, max_bytes=settings.max_body_bytes, overwrite=insert_mode is InsertMode.OVERWRITE
    )
    req = InsertIntoTableRequest(id=segments, mode=insert_mode.value, branch=branch)
    response: InsertIntoTableResponse = await run_in_threadpool(
        partial(dataplane.insert_into_table, table, so, req, data, max_bytes=settings.max_body_bytes, transaction_properties=_stamp(run, token))
    )
    # The version THIS insert committed, off the handle that committed it: a reopen would name whichever
    # append landed last.
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        location=table.location,
        so=so,
        token=token,
        operation=OVERWRITE_TABLE if insert_mode is InsertMode.OVERWRITE else INSERT,
        authorization=authorization,
        uuids=uuids,
        pin_version=response.version,
        # A branch has its own version sequence; reading this write back off main pins the edge to a
        # version that never carried it.
        branch=branch,
        run=run,
    )
    return response


@router.post("/{id}/merge_insert", response_model_exclude_none=True)
async def merge_insert_into_table(
    id: str,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    run: RunDep,
    data: Annotated[bytes, Body(media_type=ARROW_STREAM_MEDIA_TYPE)],
    # `on` STAYS A SINGLE KEY until the lance-namespace bump (A9 is blocked on A10). 0.12.0 makes it an
    # array of field paths — a composite merge key, sent as a repeated query parameter — but the
    # INSTALLED 0.11.0 model types it `str` with MinLen(1), so accepting a list here fails validation
    # inside `MergeInsertIntoTableRequest` rather than at the door. Widening the door without the
    # model would 422 a 0.12.0 client one layer deeper and read as a rask bug.
    on: str | None = None,
    when_matched_update_all: bool | None = None,
    when_matched_update_all_filt: str | None = None,
    when_not_matched_insert_all: bool | None = None,
    when_not_matched_by_source_delete: bool | None = None,
    when_not_matched_by_source_delete_filt: str | None = None,
    timeout: str | None = None,
    use_index: bool | None = None,
    branch: Annotated[str | None, Query(description="The ref to merge into. Omit for main.")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> MergeInsertIntoTableResponse:
    """Upsert Arrow-IPC rows — ``merge_insert_into_table``; emits a MERGE_INSERT lineage event.
    The ``*_filt`` SQL filters, ``timeout``, ``use_index`` and ``branch`` are spec-0.9 query params.

    The run the request's Context names is stamped into the commit, on main only, and its inputs become the
    event's version-pinned inputs ([[LH-225]]).

    The merge builds no index: a writer builds one on the ``on`` key with its own pylance, committed through
    ``version/create``, and the maintenance sweep lists a table whose index no longer covers its rows (ADR 0162)."""
    segments = parse_identifier(id)
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
    response: MergeInsertIntoTableResponse = await run_in_threadpool(
        partial(
            dataplane.merge_insert_into_table,
            table,
            so,
            req,
            data,
            max_bytes=settings.max_body_bytes,
            transaction_properties=_stamp(run, token),
        )
    )
    # merge can add/change columns (schema drift at this version) → record the post-write schema, read
    # PINNED at the version this merge produced so a concurrent writer can't smuggle in a later schema.
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        location=table.location,
        so=so,
        token=token,
        operation=MERGE_INSERT,
        authorization=authorization,
        uuids=uuids,
        pin_version=response.version,
        # Pinned AND branched, together: the dataplane call above already takes `branch=branch`, so a
        # trailer that does not is measuring a different dataset than the one just written.
        branch=branch,
        run=run,
    )
    return response


@router.post("/{id}/update", response_model_exclude_none=True)
async def update_table(
    id: str,
    body: UpdateTableRequest,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> UpdateTableResponse:
    """Update rows matching a predicate — ``update_table``; emits an UPDATE lineage event."""
    # LANCE-ONLY (2026-08-15 ruling). An update door is the create door's back way in: a table
    # made as Lance could otherwise be asked to change format afterwards, and the guard's contract is
    # that a format-selecting property is never silently ignored — on any door that accepts one.
    reject_unsupported_properties(body.properties)
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    response: UpdateTableResponse = await run_in_threadpool(dataplane.update_table, table, so, body)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        location=table.location,
        so=so,
        token=token,
        operation=UPDATE,
        authorization=authorization,
        uuids=uuids,
        pin_version=response.version,
        # The ref the dataplane committed to: the version above is that ref's number, and on main the
        # same number is a different snapshot.
        branch=body.branch,
    )
    return response


@router.post("/{id}/delete", response_model_exclude_none=True)
async def delete_from_table(
    id: str,
    body: DeleteFromTableRequest,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> DeleteFromTableResponse:
    """Delete rows matching a predicate — ``delete_from_table``; emits a DELETE lineage event."""
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    response: DeleteFromTableResponse = await run_in_threadpool(dataplane.delete_from_table, table, so, body)
    # A row-delete doesn't change columns, but the event at this new version still carries the
    # (unchanged) schema, so every version the feed records has one, not just writes.
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        location=table.location,
        so=so,
        token=token,
        operation=DELETE,
        authorization=authorization,
        uuids=uuids,
        pin_version=response.version,
        # The ref the dataplane committed to: the version above is that ref's number, and on main the
        # same number is a different snapshot.
        branch=body.branch,
    )
    return response


# A single-range ``bytes=`` header: ``bytes=0-3`` / ``bytes=100-`` / ``bytes=-500``. Multi-range and
# other units deliberately don't match — RFC 9110 §14.1.1 lets a server ignore a Range it doesn't
# support, so those fall through to the full 200 rather than a guessy 416.
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


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


def _serves(media_type: str) -> dict[int | str, dict[str, object]]:
    """The `responses=` a door answering BYTES needs, so its published contract matches what it sends.

    FastAPI reads the 200 content type from here and nowhere else. The `media_type` handed to the
    `Response` object is chosen at request time, long after the schema is generated — so without this
    a door answers Arrow for its whole life while `docs/catalog-openapi.json` says `application/json`,
    and `frontend/packages/api/src/generated/catalog.ts` is generated from that ([[LH-186]]).

    `{"type": "string", "format": "binary"}` is OpenAPI's way of saying "opaque bytes"; there is no
    schema for an Arrow FILE beyond its media type, and inventing one would describe the payload
    wrongly rather than leave it undescribed.
    """
    return {200: {"content": {media_type: {"schema": {"type": "string", "format": "binary"}}}}}


@management_router.get("/{id}/blobs", responses=_serves(_OCTET_STREAM), response_class=OctetStreamResponse)
async def read_table_blob(
    id: str,
    table: DoorTableDep,
    so: StorageOptionsDep,
    column: Annotated[str, Query(min_length=1)],
    row: Annotated[int, Query(ge=0)],
    version: Annotated[int | None, Query(ge=1)] = None,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    token: CurrentToken = None,
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

    ONE ROW PER REQUEST, and a client wanting the same byte range from MANY rows should not loop here
    ([[LH-035]]). pylance's ``LanceDataset.read_blob_ranges(blob_column, requests, selector=…)`` takes a
    sequence of ``(row, offset, length)`` tuples and plans them as ONE call — verified against
    pylance 11.0.0, and its signature unchanged on 13.0.0 — so N loops over this endpoint become N round trips and N independent
    plans where the batched API makes one. This door is deliberately the credential-less SINGLE-blob
    path; it is not the shape to build a scan on, and nothing here would tell a caller that.

    Authz: the router-level ``authorize`` maps the ``blobs`` suffix to reader-tier ``can_read_data``
    (same rung as ``/query``) — this endpoint serves DATA, so credential-vending tiers apply
    unchanged; it just removes the need for the credentials themselves.
    """
    blob = await run_in_threadpool(
        blob_serving.read_blob,
        table,
        so,
        column=column,
        row=row,
        version=version,
        range_spec=_parse_range(range_header),
        if_range=if_range,
    )
    # A BLOB READ IS THE MOST DISCLOSING READ THERE IS — it returns the bytes themselves — so it is
    # recorded with the COLUMN it served, which is the closest this door has to J1's "which columns".
    # Before the 416 branch on purpose: an unsatisfiable RANGE still told the caller the object's size
    # and ETag, which is a probe worth having in the log.
    audit_read(subject=_reader(token), resource=id, version=version, columns=[column])
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


def _column_names(columns: object) -> list[str] | None:
    """The column NAMES a request asked for, whatever shape the spec model wraps them in.

    `QueryTableRequestColumns` is a generated union wrapper, so the names sit under `actual_instance`
    on some spec revisions and are the object itself on others. Anything that is not a list of strings
    records nothing rather than a repr: a column field carrying `<object at 0x...>` is worse than an
    absent one, because it looks like data.
    """
    inner = getattr(columns, "actual_instance", columns)
    if isinstance(inner, list) and all(isinstance(c, str) for c in inner):
        return inner
    return None


def _reader(token: Caller | None) -> str:
    """Who is reading — the verified subject, or the service itself on an unauthenticated profile.

    Same expression the grant doors use (`members.py`), and it must never be blank: a read record whose
    subject is empty answers the one question the log exists for with silence.
    """
    return token.subject if token is not None else "system:catalog"


@router.post("/{id}/query", responses=_serves(ARROW_FILE), response_class=ArrowFileResponse)
def query_table(
    id: str, body: QueryTableRequest, table: DoorTableDep, data_ns: DataNamespaceDep, settings: SettingsDep, so: StorageOptionsDep, token: CurrentToken = None
) -> Response:
    """Run a query and return matching rows as an Arrow-IPC file — wraps ``query_table``.

    The native query names the table, so its rows are served only when the name still resolves to the location the
    door opened ([[LH-530]]).
    """
    body.id = reconcile_body_id(parse_identifier(id), body.id)
    dataplane.refuse_a_branch_this_door_cannot_honour(body.branch, door="query_table")
    dataplane.refuse_an_unbounded_boolean_chain(body.filter, field="filter")
    # PINNED TO THE VERSION WHOSE BASES WERE JUDGED ([[LH-279]]): the native query opens the table inside
    # Rust, where a planted base answered with another table's rows (lh279 m2).
    judged = table.open(so, version=body.version)
    dataplane.refuse_an_unbounded_result(judged, k=body.k, offset=body.offset, columns=body.columns, max_bytes=settings.query_max_response_mb << 20)
    body.version = int(judged.version)
    response = table.read_natively("query_table", body, via=data_ns)
    if not isinstance(response, QueryTableResponse) or not isinstance(response.data, bytes):
        raise TypeError(f"query_table must answer a QueryTableResponse carrying Arrow bytes, got {type(response).__name__}: {response!r:.200}")
    # AFTER the read, not before: an audit line for rows nobody received is a false record, and this
    # door's refusals (branch, identifier) raise above. § J1 — the estate authorized every read and then
    # forgot it happened.
    # `columns` is the SPEC's union wrapper (`QueryTableRequestColumns`), not a list of names, so it is
    # normalised here rather than typed loosely at the helper: the audit record is flat by contract,
    # and a wrapper object would land differently in every sink.
    audit_read(subject=_reader(token), resource=id, version=body.version, columns=_column_names(body.columns))
    return Response(content=response.data, media_type=ARROW_FILE)


# DUAL-MOUNTED, and the reason is upstream disagreeing with itself. The spec says POST at every
# tag from v0.9.0 to v0.12.0, and lance-namespace's own generated reqwest client sends POST — but
# the REST client pylance BUNDLES from the lance repo (`rust/lance-namespace-impls/src/rest.rs`)
# calls `get_json` for this op, and the reference server it bundles mounts it as GET. Since
# `lance_namespace.connect("rest", …)` resolves to that class, a POST-only route answered every
# Python user of the "rest" alias with FastAPI's default 405, which carries no `code` and so
# surfaces as `InternalError 18`. Serving both is the local fix; the upstream fix is one line in
# lance and is worth filing.
# TWO DECORATORS, NOT `api_route(methods=[...])`, and the difference is not style. One `api_route`
# with both methods emits ONE operationId for both, and FastAPI derives its suffix from whichever
# method it happened to register last — so the generated OpenAPI flipped between `_get` and
# `_post` between runs, which is invalid (operationIds must be unique) and made the contract gate
# flip-flop. Explicit ids keep the spec's POST canonical and name the GET for what it is.
@management_router.post("/{id}/changes", responses=_serves(ARROW_FILE), response_class=ArrowFileResponse)
def table_changes(id: str, body: TableChangesRequest, table: DoorTableDep, so: StorageOptionsDep, token: CurrentToken = None) -> Response:
    """Rows that changed in ``(begin_version, end_version]`` — Arrow-IPC, like ``query``.

    Answers the change data feed `lance_docs/file_format.md:6214-6242` documents, from one snapshot at
    the window's end, and refuses a window the version columns cannot describe; the rules and the
    consumer contract live in `catalog.services.changes`, the read in `dataplane.read_changes`.

    A CHANGE FEED IS A READ, which settles both policy questions: it is gated like one and audited like
    one (§ J1), because following every row a table ever received is the most disclosing read
    available, not a metadata lookup.

    THE PUSH TRIGGER IS `table_published` ([[LH-091]]), and a BYO consumer needs no new event to find
    it. That `ControlAction` (`service_kit/control_events.py:101`) announces a version advance and
    carries `{from_version, to_version}` — "the RANGE (D-R3) the notification carries, so a consumer
    resolves the delta" (`catalog/services/publication.py:245-254`) — which is exactly the window this
    door takes. Its emitter is a DIFFERENT `publication.py`, the route module
    `catalog/api/v1/endpoints/publication.py:343-356`, gated on `result.advanced` so only a real
    advance signals. A consumer subscribes to `catalog.control.v1`, reads the pair off the event, and
    calls here with it — no new `ControlAction` member, so no added control-buffer cost.
    It is deliberately NOT in notifications' `NAMED_ACTIONS`: a version advance changes an OBJECT, not
    what any particular person may do, so it is an untargeted control event like the other 31 and
    reaches a feed rather than a person.

    THE GATE IS NOT AUTOMATIC — `fga_deps._DATA_READ_ACTIONS` must name `changes`, or the router
    refuses the route for every caller. Any rung above the reader's refuses the feed's whole audience
    and records a write in the audit trail: measured 2026-09-08 at the writer rung, `can_write_data
    ALLOW` beside the `read_data` record for the same call. Pinned by
    `tests/unit/test_fga_model_contract.py::test_every_DATA_READ_door_is_gated_as_a_READ`.
    """
    # ONE CALL FOR ALL THREE KINDS: the window is one snapshot, opened at its end, and `deleted` is
    # answered from the transaction range on that same handle rather than from a scan — learning which
    # rows a table LOST is as disclosing as learning which it gained, so it is audited identically.
    data = dataplane.read_changes(
        table, so, begin_version=body.begin_version, end_version=body.end_version, kind=body.kind, columns=body.columns, branch=body.branch
    )
    audit_read(subject=_reader(token), resource=id, version=body.end_version, columns=body.columns, change_kind=body.kind)
    # STREAMED, like the blob door above and for the same reason: a feed's size is a property of the
    # DATA and this door has no bound to offer — the version window is the only cursor, and one
    # version can carry the whole table. The audit is emitted BEFORE the first byte on purpose: the
    # disclosure is the request, and a consumer that hangs up mid-stream has still made it.
    #
    # RETURNED rather than declared as `response_class=` + `yield from`, which is the form the fastapi
    # skill prefers — and that preference is wrong for a producer that VALIDATES. Measured 2026-09-22
    # on this FastAPI: with `yield from`, the endpoint IS the generator, so a raise from the producer
    # lands after the response has begun and the caller receives **200 with an empty body**; returning
    # the response calls the producer in the endpoint body, where the same raise is a 500 that
    # `install_problem_handlers` turns into the 400 the caller can act on. `read_changes` pulls its
    # first batch eagerly for exactly that reason, and this line is the other half of it.
    return StreamingResponse(data, media_type=ARROW_FILE)


@router.post("/{id}/count_rows", operation_id="count_table_rows")
@router.get("/{id}/count_rows", operation_id="count_table_rows_compat_get")
def count_table_rows(
    id: str,
    table: DoorTableDep,
    data_ns: DataNamespaceDep,
    so: StorageOptionsDep,
    body: CountTableRowsRequest | None = None,
    token: CurrentToken = None,
) -> Response:
    """Count the table's rows on the ref the request names — ``count_table_rows``; returns plain text.

    Routed through `dataplane` rather than straight to `native.call` so that `branch` is honoured. It
    was not: the parameter reached the upstream implementation, which answered from main regardless,
    so a branch-scoped count returned a plausible number for the wrong dataset with a 200.
    """
    req = body or CountTableRowsRequest()
    req.id = reconcile_body_id(parse_identifier(id), req.id)
    # A JSON INTEGER, per `components.responses.CountTableRowsResponse`: "Row count serialized
    # transparently as a bare number for the REST namespace." It answered `text/plain` and survived
    # only by accident — a bare number happens to parse as JSON — while its two siblings below, whose
    # payload is a STRING, did not.
    counted = dataplane.count_rows(table, so, req, data_ns=data_ns)
    # A COUNT IS A READ. It answers a question about the rows without returning them, and a subject that
    # can count is a subject that can probe — so it belongs in the same log as a query (§ J1).
    audit_read(subject=_reader(token), resource=id, version=getattr(req, "version", None))
    return JSONResponse(content=counted)


@router.post("/{id}/explain_plan")
def explain_table_query_plan(id: str, body: ExplainTableQueryPlanRequest, table: DoorTableDep, so: StorageOptionsDep, token: CurrentToken = None) -> Response:
    """Return the logical query plan — ``explain_table_query_plan``; plain text, served once the name still resolves to the door's location."""
    body.id = reconcile_body_id(parse_identifier(id), body.id)
    # BOTH CHANNELS. `ExplainTableQueryPlanRequest` nests the whole query, so a branch can arrive at
    # the top level OR inside `query` — and guarding only the outer one left the inner one serving
    # main's plan with a 200 for a branch that had never been created. A guard that covers one of two
    # channels reads as a guard while being none.
    dataplane.refuse_a_branch_this_door_cannot_honour(body.branch or (body.query.branch if body.query else None), door="explain_table_query_plan")
    dataplane.refuse_an_unbounded_boolean_chain(body.query.filter if body.query else None, field="query.filter")
    judged = int(table.open(so, version=body.query.version if body.query else None).version)
    if body.query is not None:
        body.query.version = judged
    result = table.read_natively("explain_table_query_plan", body)
    # A JSON-ENCODED STRING, per `components.responses`. It answered `text/plain`, which the 0.12.0
    # reqwest client rejects outright (the Python urllib3 client tolerates it, which is why rask's own
    # e2e never noticed). `JSONResponse` on a `str` encodes it AS a string, so a plan that happens to
    # look like JSON round-trips as the opaque text it is rather than being mistaken for structure.
    plan = result if isinstance(result, str) else json.dumps(dump(result))
    # AN EXPLAIN IS A READ OF THE SHAPE, not of the rows: it discloses the columns, the filters and
    # the plan the engine would run — exactly what a subject probing a table it may not query would
    # ask for. § J1 counts it as a read for that reason.
    audit_read(subject=_reader(token), resource=id, columns=_column_names(getattr(body, "columns", None)))
    return JSONResponse(content=plan)


@router.post("/{id}/analyze_plan")
def analyze_table_query_plan(
    id: str, body: AnalyzeTableQueryPlanRequest, table: DoorTableDep, data_ns: DataNamespaceDep, settings: SettingsDep, so: StorageOptionsDep
) -> Response:
    """Return the analyzed query plan with runtime metrics — ``analyze_table_query_plan``; plain text, served once the name still resolves to the door's location."""
    body.id = reconcile_body_id(parse_identifier(id), body.id)
    dataplane.refuse_a_branch_this_door_cannot_honour(body.branch, door="analyze_table_query_plan")
    dataplane.refuse_an_unbounded_boolean_chain(body.filter, field="filter")
    # ANALYZE RUNS THE QUERY to report its metrics, so it spends what `/query` would and takes its bound.
    judged = table.open(so, version=body.version)
    dataplane.refuse_an_unbounded_result(judged, k=body.k, offset=body.offset, columns=body.columns, max_bytes=settings.query_max_response_mb << 20)
    body.version = int(judged.version)
    result = table.read_natively("analyze_table_query_plan", body, via=data_ns)
    # A JSON-ENCODED STRING, per `components.responses`. It answered `text/plain`, which the 0.12.0
    # reqwest client rejects outright (the Python urllib3 client tolerates it, which is why rask's own
    # e2e never noticed). `JSONResponse` on a `str` encodes it AS a string, so a plan that happens to
    # look like JSON round-trips as the opaque text it is rather than being mistaken for structure.
    plan = result if isinstance(result, str) else json.dumps(dump(result))
    return JSONResponse(content=plan)
