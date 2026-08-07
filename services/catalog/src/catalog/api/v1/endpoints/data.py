"""Table data endpoints: Arrow-IPC writes, query, count, update/delete, plans, blob serving."""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Annotated, Any

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
    DropTableRequest,
    ExplainTableQueryPlanRequest,
    InsertIntoTableRequest,
    InsertIntoTableResponse,
    InvalidInputError,
    LanceNamespace,
    MergeInsertIntoTableRequest,
    MergeInsertIntoTableResponse,
    QueryTableRequest,
    TableNotFoundError,
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
from catalog.core.control_emit import emit_control
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.core.lineage_emit import DELETE, INSERT, MERGE_INSERT, UPDATE, InputPin, InputRef, shape_run_facets
from catalog.core.lineage_metadata import build_lineage_metadata, inject_into_arrow_stream
from catalog.core.serialization import dump
from catalog.schemas import CommitFragmentsRequest, CommitFragmentsResponse
from catalog.services import dataplane, native
from service_kit.governed import fga


log = logging.getLogger(__name__)


ARROW_STREAM = "application/vnd.apache.arrow.stream"
ARROW_FILE = "application/vnd.apache.arrow.file"

# Cap on the create payload we'll decode→re-encode in-process to stamp lineage metadata (#21). Above
# this we skip the stamp (the graph still gets the create run); keeps a large create off a ~3x-memory
# re-encode on the request path. (#22 audit)
_MAX_INJECT_BYTES = 64 * 1024 * 1024

router = APIRouter(prefix="/v1/table", tags=["data"])


def _compensation_allowed(mode: str | None, overwrote_existing: bool) -> bool:
    """Whether a failed owner grant may compensate by DROPPING the table — only for a FRESH id.

    Never for ExistOk (it may have KEPT a pre-existing table this request never wrote) and never for
    an Overwrite that REPLACED an existing table (the id still holds the prior incarnation's
    time-travel history; dropping would escalate a transient FGA blip into irreversible data loss —
    review 2026-07-10). Pure so the Overwrite arm is unit-testable (it needs FGA on, unreachable in
    the moto harness).
    """
    return (mode or "").lower() not in ("existok", "exist_ok") and not overwrote_existing


def _table_exists(ns: LanceNamespace, segments: list[str]) -> bool:
    """True if a table already lives at ``segments`` (declared-only counts — it already holds an owner
    grant). Used to decide whether a create ``mode=Overwrite`` is destroying an EXISTING table (which then
    needs an owner-tier gate) vs creating a fresh one. Blocking native call → run in a threadpool."""
    try:
        native.call(ns, "describe_table", DescribeTableRequest(id=segments, check_declared=True))
        return True
    except TableNotFoundError:
        return False


# #78 format-selecting properties an Iceberg / Unity-Catalog client might send, expecting to choose a file
# format. This catalog stores Lance ONLY (columnar, self-describing, versioned), so honouring them is
# impossible — echoing them back would let the client believe it got a format it did not.
_FORMAT_KEYS = ("write.format.default", "data_source_format")


def _reject_unsupported_format(properties: object) -> None:
    """Raise 400 if the create ``properties`` request a non-Lance file format — never a silent no-op."""
    if not isinstance(properties, dict):
        return
    for key in _FORMAT_KEYS:
        requested = properties.get(key)
        if requested is not None and str(requested).lower() != "lance":
            raise InvalidInputError(
                f"file format {requested!r} ({key}) is not supported — this catalog stores Lance only; format-selecting properties are not silently ignored"
            )


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
    data: Annotated[bytes, Body(media_type=ARROW_STREAM)],
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
    # #118: this door had NO parent guard at all — require_parent lives in tables.py and this route
    # lives here, so the Arrow create wrote real datasets into namespaces that do not exist, with a
    # live owner grant and no parent edge. Checked BEFORE the write: a refusal leaves nothing.
    await fga_deps.require_parent_exists(ns, "table", parse_identifier(id, settings.delimiter), delimiter=settings.delimiter)

    # #3-B governance (the security crux): validate BEFORE any write. An off-allowlist base is a client
    # error (400), never a silent write to an unapproved bucket.
    if data_base:
        approved = set(settings.multibase_data_base_list)
        rogue = [b for b in data_base if b not in approved]
        if rogue:
            raise InvalidInputError(f"data_base(s) not in the LANCE_MULTIBASE_DATA_BASES allowlist: {rogue}")
    parsed_properties = None
    if properties:
        try:
            parsed_properties = json.loads(properties)
        except json.JSONDecodeError as exc:
            raise InvalidInputError(f"table properties is not valid JSON: {exc}") from exc
    # #78 format honesty: reject a client that tries to select another file format (see the helper).
    _reject_unsupported_format(parsed_properties)
    # S4: validate the optional lineage metadata BEFORE the write — a malformed pin/facet is a 4xx,
    # not a committed create whose provenance then silently drops. Same order, same helpers, same
    # forge-guard as merge_insert: a caller who cannot READ the named source must not be able to
    # stamp a cross-tenant DERIVED_FROM edge (or a phantom vertex) into trusted lineage.
    source_pin = _merge_source_pin(source, source_version, settings.delimiter)
    extra_run_facets = _parse_run_facets(run_facets_json)
    if source_pin is not None:
        await fga_deps.require_can_get_metadata(client, settings, token, segments=source_pin.segments)
    segments = parse_identifier(id, settings.delimiter)
    table_id = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    namespace = fga.parent_namespace_id(segments, delimiter=settings.delimiter) or ""
    created_by = token.sub if token is not None else None
    run_id = str(uuid.uuid4())
    # #21: stamp the lineage coordinates into the Lance file's schema metadata so the data is
    # self-describing (reconcilable to the graph without the catalog). Best-effort — a payload we
    # can't re-encode must never fail the create over metadata; fall back to the original bytes.
    # Gated on lineage being enabled (the inject is a full Arrow decode→re-encode, ~3x the payload
    # in memory) and a size ceiling (don't re-encode an arbitrarily large body in-process); when off
    # or oversized we don't stamp a create_run_id the graph never receives. (#22 audit)
    if settings.lineage_emit_enabled and len(data) <= _MAX_INJECT_BYTES:
        try:
            data = await run_in_threadpool(
                inject_into_arrow_stream,
                data,
                build_lineage_metadata(table_id=table_id, namespace=namespace, run_id=run_id),
            )
        except Exception as exc:
            log.warning("lineage_metadata_inject_failed", extra={"table": table_id, "error": str(exc)})
    # mode=Overwrite is spec-defined as "the existing table is DROPPED and a new table created" (lance
    # namespace.md). ``authorize`` only gated this create at writer-tier can_create_table on the PARENT — but
    # a DROP needs owner-tier can_drop. So if an Overwrite is about to DESTROY an existing table, require
    # owner-tier on it FIRST (before the irreversible write) — else a mere namespace writer could overwrite
    # and, via the ownership reset below, seize another user's table. Fresh-id Overwrite creates nothing to
    # gate. FGA-off skips it (no ACL to protect).
    # Pre-existence (declared-only counts — it already holds an owner grant), computed BEFORE the write and
    # only when FGA is on (the ACLs it protects exist only then). It feeds TWO owner-tier guards:
    #   · Overwrite of an EXISTING table is a DROP → needs owner-tier can_drop first, else a namespace writer
    #     could overwrite and, via the ownership reset below, SEIZE another user's table.
    #   · ExistOk that KEEPS an existing table wrote NOTHING — so seeding the caller `owner` would let ANY
    #     authenticated user (or namespace-writer) SEIZE ownership of an already-owned table via a no-op
    #     create (audit: CRITICAL). We must never grant owner on a table this request did not create.
    # The describe (_table_exists) runs only for Overwrite/ExistOk with FGA on.
    _mode = (mode or "").lower()
    pre_existed = (
        _mode in ("overwrite", "existok", "exist_ok") and settings.fga_enabled and client is not None and await run_in_threadpool(_table_exists, ns, segments)
    )
    overwrote_existing = pre_existed and _mode == "overwrite"
    existok_kept_existing = pre_existed and _mode in ("existok", "exist_ok")
    if overwrote_existing:
        await fga_deps.require_can_drop_table(client, settings, token, segments=segments)
    # ``dataplane.create_table`` picks the write path by schema off the event loop: a blob-v2 column needs
    # file format 2.2 (native create pins 2.1 and rejects it) → a direct 2.2 write; else → native create. (§9)
    response: CreateTableResponse = await run_in_threadpool(
        dataplane.create_table,
        ns,
        so,
        segments,
        data,
        mode=mode,
        properties=parsed_properties,
        allow_external_blobs=settings.allow_external_blobs,
        external_blob_bases=settings.external_blob_base_list,
        data_bases=data_base or None,
    )
    # An Overwrite that replaced an EXISTING table (owner-authorized above) resets its ACL: revoke the prior
    # incarnation's grants (any reader/writer/validator that must not survive onto the reused id) before
    # re-seeding the overwriter. Only when we actually overwrote — a fresh create has nothing to revoke, and
    # revoking on a non-owner path is what the audit flagged as an eviction vector (now gated out).
    if overwrote_existing:
        await fga_deps.revoke_ownership(client, settings, resource="table", segments=segments, token=token)
    # Make the caller owner + link the new table to its parent so it inherits the cascade.
    # COMPENSATION (§4 dual-write): if the grant fails here (FGA outage → 503), the table exists on
    # storage but has NO owner tuple — the client's retry would hit "already exists", stranding it
    # forever. Best-effort delete what THIS request wrote so the retry starts clean — but ONLY for a
    # FRESH id (review 2026-07-10): never for ExistOk (it may have KEPT a pre-existing table this
    # request never wrote) and never when Overwrite REPLACED an existing table (the id still holds the
    # prior incarnation's time-travel history — a compensating drop would escalate a transient FGA
    # blip into irreversible loss; stranded-but-admin-recoverable beats destroyed). The compensation
    # also REVOKES any tuples that did land (a grant can commit server-side while its response is
    # lost; a stale owner tuple on a freed id silently grants its holder the NEXT table created there
    # — the reused-id privilege bleed the real drop path also guards).
    # Residual (documented): a process CRASH between the write and the grant still strands the table
    # (no in-process compensation can cover it); the deeper fix is a declare→grant→write reorder.
    # Seed ownership ONLY for a table THIS request actually created. An ExistOk that KEPT an already-existing
    # table wrote nothing, so granting the caller `owner` would seize another user's table (audit: CRITICAL) —
    # the existing owner (or the /declare-r of a declared-only table) keeps ownership. Skipping the seed also
    # skips the compensation (there is nothing this request wrote to compensate).
    if not existok_kept_existing:
        try:
            await fga_deps.seed_ownership(client, settings, token, resource="table", segments=segments)
        except Exception:
            if _compensation_allowed(mode, overwrote_existing):
                try:  # compensation is best-effort; the GRANT error below stays the response either way
                    await fga_deps.revoke_ownership(client, settings, resource="table", segments=segments, token=token)
                    await run_in_threadpool(native.call, ns, "drop_table", DropTableRequest(id=segments))
                    log.warning("create_compensated", extra={"table": table_id, "reason": "grant_failed"})
                except Exception as drop_exc:
                    log.warning("create_compensation_failed", extra={"table": table_id, "error": str(drop_exc)})
            raise
    # Record provenance authoritatively: the catalog knows the verified principal. Fire-and-forget
    # (after the response, best-effort) so the lineage service can never block/fail a create. The
    # canonical id keeps the lineage Dataset == the OpenFGA object id == the catalog table id; the
    # caller's bearer is forwarded so ingest accepts it when the lineage service has OIDC on; the
    # ``run_id`` is the same one stamped into the Lance file above (#21).
    # Inline-await (NOT BackgroundTasks — no retry, dies with the worker; fastapi anti-pattern) so the event
    # reaches the durable Dapr/JetStream transport before the response. emit_create is best-effort internally,
    # so it never fails the create; JetStream + the consumer's idempotent MERGE-on-run_id give durability.
    # The per-version column schema (blob/vector-aware) for the WROTE edge (#24). A create/Overwrite writes
    # exactly the request bytes, so the payload schema IS the table's schema — parsed in memory, no
    # describe + dataset reopen round trip. ExistOk is the exception: it may have KEPT an existing table
    # (nothing written, response.version = the existing version), so the payload schema could belong to a
    # table that was never created — read the true schema back PINNED at that version instead. Best-effort
    # either way (failure → []).
    if (mode or "").lower() in ("existok", "exist_ok"):
        _, schema_fields = await run_in_threadpool(dataplane.read_version_and_schema, ns, so, segments, response.version)
    else:
        schema_fields = await run_in_threadpool(dataplane.payload_schema_fields, data, segments)
    # S4: the pin resolves to a version-pinned INPUT exactly as `emit_write_event` resolves a merge's
    # (canonical ids, so the lineage Dataset == the OpenFGA object); the facets ride verbatim.
    input_refs = (
        [
            InputRef(
                fga.parent_namespace_id(source_pin.segments, delimiter=settings.delimiter) or "",
                fga.canonical_object_id(source_pin.segments, delimiter=settings.delimiter),
                source_pin.version,
            )
        ]
        if source_pin is not None
        else None
    )
    await emitter.emit_create(
        table_id=table_id,
        namespace=namespace,
        author=created_by,
        version=response.version or 1,
        run_id=run_id,
        authorization=authorization,
        source_uri=response.location,  # the real Lance URI → #23 reconcile can read the on-disk file
        schema_fields=schema_fields,
        inputs=input_refs,
        extra_run_facets=extra_run_facets,
    )
    # Only a real creation emits — an ExistOk request that KEPT a pre-existing table wrote nothing and
    # created nothing (same guard that skips ownership seeding above), so a `table_created` here would be a
    # spurious event announcing a creation-by-caller that never happened. A fresh create + an Overwrite
    # (drop+recreate) still emit.
    if not existok_kept_existing:
        await emit_control(
            control,
            action="table_created",
            object_type="table",
            object_id=f"table:{table_id}",
            actor=f"user:{token.sub}" if token is not None else None,
            extra={
                "namespace": namespace,
                "version": response.version or 1,
                "mode": mode,
                "location": response.location,
            },
        )
    return response


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
    version, row_count = await run_in_threadpool(
        dataplane.commit_appended_fragments, described.location, so, body.fragments, body.read_version, body.run_id
    )
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
    data: Annotated[bytes, Body(media_type=ARROW_STREAM)],
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
    response: InsertIntoTableResponse = await run_in_threadpool(native.call, ns, "insert_into_table", req, data)
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


def _merge_source_pin(source: str | None, source_version: int | None, delimiter: str) -> InputPin | None:
    """The optional version-pinned source dataset a merge DERIVED FROM → an ``InputPin`` (or ``None``).

    A blank/absent ``source`` yields no pin; a ``source_version`` with no ``source`` is meaningless (there
    is nothing to pin) → a fail-fast 4xx, not a silently dropped pin. An empty/delimiter-only ``source`` is
    rejected too — otherwise it would forge a nameless input vertex (or, on the HTTP transport, drop the
    whole event). The pin makes the merge reproducible: its provenance names the exact source version
    consumed, surfaced on the lineage READ edge (``input_version``)."""
    if not source or not source.strip():
        if source_version is not None:
            raise InvalidInputError("source_version requires source")
        return None
    segments = parse_identifier(source, delimiter)
    if not segments or any(not segment for segment in segments):
        raise InvalidInputError(f"source is not a valid dataset id: {source!r}")
    return InputPin(segments=segments, version=source_version)


def _parse_run_facets(raw_json: str | None) -> dict[str, Any] | None:
    """Parse + spec-shape the optional ``X-Lance-Run-Facets`` header (a JSON object ``{facet: {payload}}``).

    The Arrow-IPC body carries the merge data, so a producer's run metadata (e.g. training ``params``)
    rides this header instead. The catalog stays un-opinionated about the payload (:func:`shape_run_facets`
    only stamps each facet spec-legal + rejects reserved facet names); malformed JSON, a non-object shape,
    or a reserved name/key fail-fast as a 4xx (never a 500)."""
    if not raw_json:
        return None
    try:
        parsed = json.loads(raw_json)
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError ⊂ ValueError, but json.loads also raises a BARE ValueError for an integer literal
        # past the 4300-digit int-string-conversion limit, and RecursionError for a deeply-nested payload —
        # both a malformed producer header (a 4xx, never a 500). (Phase 2 re-audit, 2026-07-21.)
        raise InvalidInputError("X-Lance-Run-Facets must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise InvalidInputError("X-Lance-Run-Facets must be a JSON object of {facet: {payload}}")
    try:
        return shape_run_facets(parsed)
    except (ValueError, TypeError) as exc:  # TypeError: belt-and-suspenders for a payload kwarg collision
        raise InvalidInputError(str(exc)) from exc


@router.post("/{id}/merge_insert", response_model_exclude_none=True)
async def merge_insert_into_table(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    client: FgaClientDep,
    data: Annotated[bytes, Body(media_type=ARROW_STREAM)],
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
    source_pin = _merge_source_pin(source, source_version, settings.delimiter)
    extra_run_facets = _parse_run_facets(run_facets_json)
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
    response: MergeInsertIntoTableResponse = await run_in_threadpool(native.call, ns, "merge_insert_into_table", req, data)
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
        await run_in_threadpool(dataplane.ensure_merge_key_index, ns, segments, on, branch=branch)
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
        dataplane.read_blob,
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
    data = native.call(ns, "query_table", body)
    return Response(content=data, media_type=ARROW_FILE)


@router.post("/{id}/count_rows")
def count_table_rows(id: str, ns: NamespaceDep, settings: SettingsDep, body: CountTableRowsRequest | None = None) -> Response:
    """Count the table's rows (optionally filtered) — ``count_table_rows``; returns plain text."""
    req = body or CountTableRowsRequest()
    req.id = reconcile_body_id(parse_identifier(id, settings.delimiter), req.id)
    count = native.call(ns, "count_table_rows", req)
    return PlainTextResponse(str(count))


@router.post("/{id}/explain_plan")
def explain_table_query_plan(id: str, body: ExplainTableQueryPlanRequest, ns: NamespaceDep, settings: SettingsDep) -> Response:
    """Return the logical query plan — ``explain_table_query_plan``; plain text."""
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    result = native.call(ns, "explain_table_query_plan", body)
    return PlainTextResponse(result if isinstance(result, str) else json.dumps(dump(result)))


@router.post("/{id}/analyze_plan")
def analyze_table_query_plan(id: str, body: AnalyzeTableQueryPlanRequest, ns: NamespaceDep, settings: SettingsDep) -> Response:
    """Return the analyzed query plan with runtime metrics — ``analyze_table_query_plan``; plain text."""
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    result = native.call(ns, "analyze_table_query_plan", body)
    return PlainTextResponse(result if isinstance(result, str) else json.dumps(dump(result)))
