"""The GOVERNED create — every step of ``POST /v1/table/{id}/create`` except the routing.

Moved out of ``api/v1/endpoints/data.py`` (catalog-api-03), where it was fourteen awaits and fifty
statements inline in the handler against a module median of two. ``schemas.py``'s own header states the
intent for that plane — "the endpoints stay routing-only" — and this is the sequence that never left.

The steps, and the order, are exactly what the door ran, because the ORDER is the contract:

1. shape guards that cost nothing (wildcards, the multi-base allowlist, ``properties`` as a map of
   strings, the LANCE-ONLY format rule, the derived-write pin and the run facets) —
   :func:`parse_create_shape` — and the body read as an Arrow IPC stream
   (``dataplane.read_arrow_body``). The door runs both before its idempotency claim, so a request that
   is invalid on its face never pays for a lookup (catalog-api-19) and never holds a key;
2. the parent-exists and live-trash guards — round trips, still strictly BEFORE the write;
3. the derived-write pin, authorized before the write rather than after it;
4. the #21 lineage stamp into the decoded table's schema metadata;
5. the pre-existence probe and its owner-tier drop gate, which is what stops a namespace writer
   seizing a table through Overwrite;
6. the data-plane write;
7. the ACL reset for an Overwrite, then seed-with-compensation;
8. the schema read-back, the lineage emit and the control emit.

**Why this is a service module and not a second endpoint helper.** The compensation rules — never drop
for ExistOk, never drop an Overwrite that replaced a table — are the highest-consequence decisions in
the catalog, and while they lived in a handler the only way to exercise them was through an HTTP door;
``test_compensation_matrix_never_drops_a_replaced_or_kept_table`` says so in its own docstring. Here
they are callable directly.

It imports ``catalog.api.fga_deps`` for the authorization seam, exactly as
``catalog.services.cascade_backfill`` already does: the guards are the estate's one implementation and
re-deriving them here would be the weakened duplicate the audit keeps finding elsewhere.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pyarrow as pa
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    CreateTableResponse,
    DescribeTableRequest,
    DropTableRequest,
    InvalidInputError,
    LanceNamespace,
    TableNotFoundError,
)
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel

from catalog.api import fga_deps
from catalog.core.config import Settings
from catalog.core.formats import reject_unsupported_format
from catalog.core.identifiers import parse_identifier, require_safe_segments
from catalog.core.lineage_emit import InputPin, InputRef, LineageEmitter, merge_source_pin, parse_run_facets
from catalog.core.lineage_metadata import build_lineage_metadata, stamp_lineage_metadata
from catalog.core.modes import CreateMode
from catalog.services import dataplane, native
from service_kit.control_emit import ControlEmitter, emit_control
from service_kit.governed import fga
from service_kit.governed.oidc import IDToken
from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)


def compensation_allowed(mode: CreateMode, overwrote_existing: bool) -> bool:
    """Whether a failed owner grant may compensate by DROPPING the table — only for a FRESH id.

    Never for ExistOk (it may have KEPT a pre-existing table this request never wrote) and never for
    an Overwrite that REPLACED an existing table (the id still holds the prior incarnation's
    time-travel history; dropping would escalate a transient FGA blip into irreversible data loss —
    review 2026-07-10). Pure so the Overwrite arm is unit-testable (it needs FGA on, unreachable in
    the moto harness).
    """
    return mode is not CreateMode.EXIST_OK and not overwrote_existing


def table_exists(ns: LanceNamespace, segments: list[str]) -> bool:
    """True if a table already lives at ``segments`` (declared-only counts — it already holds an owner
    grant). Used to decide whether a create ``mode=Overwrite`` is destroying an EXISTING table (which then
    needs an owner-tier gate) vs creating a fresh one. Blocking native call → run in a threadpool."""
    try:
        native.call(ns, "describe_table", DescribeTableRequest(id=segments, check_declared=True))
        return True
    except TableNotFoundError:
        return False


class CreateShape(BaseModel):
    """What the create door decoded from the request itself, before its idempotency claim."""

    #: spec.yaml:3761-3767 types it as an object whose values are strings.
    properties: dict[str, str] | None = None
    source_pin: InputPin | None = None
    run_facets: dict[str, Any] | None = None


def _parse_properties(raw: str | None) -> dict[str, str] | None:
    """The ``properties`` query parameter as the spec types it, or a 400 naming what it is instead."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        # ValueError covers JSONDecodeError and the bare one CPython raises for an integer past its
        # 4300-digit conversion limit; RecursionError a deeply nested value. All three are the caller's.
        raise InvalidInputError(f"table properties is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise InvalidInputError(f"table properties must be a JSON object of string values, got {type(parsed).__name__}")
    properties: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(value, str):
            raise InvalidInputError(f"table properties must be a JSON object of string values; {key!r} is {type(value).__name__}")
        properties[key] = value
    return properties


def parse_create_shape(
    id: str,
    *,
    settings: Settings,
    data_base: list[str],
    properties: str | None,
    source: str | None,
    source_version: int | None,
    run_facets_json: str | None,
) -> CreateShape:
    """Refuse a create that is malformed on its face, and return what it decoded.

    Pure: no round trip, no write. The door calls it BEFORE its idempotency claim, beside the mode
    parse, because a claim minted for a request refused here would hold the key for its lease and
    answer the corrected retry 409. Authorizing the pin needs FGA, so that stays in
    :func:`create_governed_table`.
    """
    # A wildcard (`*`/`?`) in a segment would flow verbatim from the table's derived prefix into the
    # vended STS session policy and widen credentials to siblings — refused at SHAPE, before any write.
    require_safe_segments(parse_identifier(id, settings.delimiter), delimiter=settings.delimiter)
    # #3-B governance (the security crux): validate BEFORE any write. An off-allowlist base is a client
    # error (400), never a silent write to an unapproved bucket.
    if data_base:
        approved = set(settings.multibase_data_base_list)
        rogue = [b for b in data_base if b not in approved]
        if rogue:
            raise InvalidInputError(f"data_base(s) not in the LANCE_MULTIBASE_DATA_BASES allowlist: {rogue}")
    parsed_properties = _parse_properties(properties)
    # #78 format honesty: reject a client that tries to select another file format (see the helper).
    reject_unsupported_format(parsed_properties)
    return CreateShape(
        properties=parsed_properties,
        source_pin=merge_source_pin(source, source_version, settings.delimiter),
        run_facets=parse_run_facets(run_facets_json),
    )


async def create_governed_table(
    *,
    id: str,
    ns: LanceNamespace,
    settings: Settings,
    token: IDToken | None,
    client: OpenFgaClient | None,
    emitter: LineageEmitter,
    control: ControlEmitter,
    so: StorageOptions,
    table: pa.Table,
    mode: CreateMode,
    shape: CreateShape,
    data_base: list[str],
    authorization: str | None,
) -> CreateTableResponse:
    """Create a Lance table from an Arrow-IPC stream, governed end to end.

    ``mode``, ``shape`` and ``table`` arrive PARSED ONCE, by the door, which refuses a malformed one
    before its idempotency claim (``CreateMode.parse``, :func:`parse_create_shape`,
    ``dataplane.read_arrow_body``); four decisions below turn on
    the mode — the pre-existence guards, the ownership seed, the schema read-back and the compensation
    rule. The derived-write pin is AUTHORIZED here, after the round trips and before the write — see
    the module docstring.
    """
    # THE ROUND TRIPS COME AFTER THE FREE CHECKS (catalog-api-19). These two both dial out — a
    # describe against the namespace backend and a trash-registry read on the object store — and they
    # used to be the handler's FIRST two statements, so the commonest way to get a create wrong (a
    # typo'd `data_base`, unparseable `properties`) cost two network round trips before the server
    # said what was actually wrong, and an outage of either answered 503/404 for a request that is
    # invalid on its face. Still strictly BEFORE the write, which is the property that matters: a
    # refusal here leaves nothing behind.
    #
    # #118: this door had NO parent guard at all — require_parent lives in tables.py and this route
    # lives here, so the Arrow create wrote real datasets into namespaces that do not exist, with a
    # live owner grant and no parent edge.
    await fga_deps.require_parent_exists(ns, "table", parse_identifier(id, settings.delimiter), delimiter=settings.delimiter)
    # The id must not still belong to a trashed table (diff2 F10 item 4): a recoverable drop KEEPS
    # its grants, so creating here would hand the new table the dead one's readers and writers.
    await fga_deps.require_no_live_trash(settings, parse_identifier(id, settings.delimiter))
    # S4: authorize the parsed pin BEFORE the write, with the same forge-guard as merge_insert: a
    # caller who cannot READ the named source must not be able to stamp a cross-tenant DERIVED_FROM
    # edge (or a phantom vertex) into trusted lineage.
    source_pin = shape.source_pin
    if source_pin is not None:
        await fga_deps.require_can_get_metadata(client, settings, token, segments=source_pin.segments)
    segments = parse_identifier(id, settings.delimiter)
    table_id = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    namespace = fga.parent_namespace_id(segments, delimiter=settings.delimiter) or ""
    created_by = token.sub if token is not None else None
    run_id = str(uuid.uuid4())
    # #21: stamp the lineage coordinates into the Lance file's schema metadata so the data is
    # self-describing (reconcilable to the graph without the catalog). A metadata swap on the decoded
    # table that shares its buffers, so no payload is too large to stamp. Gated on lineage being
    # enabled: when off we don't stamp a create_run_id the graph never receives.
    if settings.lineage_emit_enabled:
        table = stamp_lineage_metadata(table, build_lineage_metadata(table_id=table_id, namespace=namespace, run_id=run_id))
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
    # The describe (table_exists) runs only for Overwrite/ExistOk with FGA on.
    pre_existed = mode is not CreateMode.CREATE and settings.fga_enabled and client is not None and await run_in_threadpool(table_exists, ns, segments)
    overwrote_existing = pre_existed and mode is CreateMode.OVERWRITE
    existok_kept_existing = pre_existed and mode is CreateMode.EXIST_OK
    if overwrote_existing:
        await fga_deps.require_can_drop_table(client, settings, token, segments=segments)
    # ``dataplane.create_table`` picks the write path by schema off the event loop: a blob-v2 column needs
    # file format 2.2 (native create pins 2.1 and rejects it) → a direct 2.2 write; else → native create. (§9)
    response: CreateTableResponse = await run_in_threadpool(
        dataplane.create_table,
        ns,
        so,
        segments,
        table,
        mode=mode,
        properties=shape.properties,
        allow_external_blobs=settings.allow_external_blobs,
        external_blob_bases=settings.external_blob_base_list,
        data_bases=data_base or None,
        # [[LH-067]] WHICH secret each base's credential comes from. The map holds NAMES; the material
        # is fetched at composition through the Dapr store the catalog already uses for its own S3
        # secret, so nothing secret passes through here. Empty by default, which renders exactly
        # today's map.
        base_credential_refs=settings.multibase_base_credential_ref_map,
        secret_store=settings.dapr_secret_store,
        secret_field=settings.dapr_secret_s3_field,
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
    async def _undo_create() -> None:
        await run_in_threadpool(native.call, ns, "drop_table", DropTableRequest(id=segments))

    # The revoke-then-drop pair moved into `seed_ownership_or_compensate` (diff2 F3) because it was
    # ONE try block here: the revoke is an OpenFGA call, so on the outage this compensation exists
    # for, it raised and the native drop never ran. Now they are independent best-effort steps and
    # the drop — which needs no FGA — always gets its turn.
    #
    # THE SEED IS UNCONDITIONAL AND THE OWNER GRANT IS THE FLAG, which is the distinction
    # `grant_on_create` was given `grant_owner` for: dropping the owner tuple and dropping the `parent`
    # EDGE look identical at a call site and are opposite in effect. An ExistOk that KEPT an existing
    # table must not grant the caller `owner` — that is an ownership seizure, audited CRITICAL — but
    # its edge is not an ownership question at all: it names where the table lives, it is idempotent,
    # and it confers nothing alone. Skipping the whole call withheld both.
    #
    # Measured 2026-09-15: `ensure_stage_output` is describe-then-create-with-ExistOk, so once a table
    # existed without an edge every later registration skipped the seed again and it could never
    # acquire one. Five of the cascade's own governed tiers held ZERO tuples — no owner, no parent —
    # and `table.maintainer`/`owner`/every other relation resolve through `... from parent`, so they
    # were unreachable from every container grant: unmaintainable, ungrantable, unprotectable.
    #
    # `undo` stays conditional. Compensation undoes what THIS request created, and an ExistOk that kept
    # an existing table created nothing to undo.
    seeding_a_new_table = not existok_kept_existing
    await fga_deps.seed_ownership_or_compensate(
        client,
        settings,
        token,
        resource="table",
        segments=segments,
        may_grant_owner=seeding_a_new_table,
        undo=_undo_create if (seeding_a_new_table and compensation_allowed(mode, overwrote_existing)) else None,
    )
    # Record provenance authoritatively: the catalog knows the verified principal. Fire-and-forget
    # (after the response, best-effort) so the lineage service can never block/fail a create. The
    # canonical id keeps the lineage Dataset == the OpenFGA object id == the catalog table id; the
    # caller's bearer is forwarded so ingest accepts it when the lineage service has OIDC on; the
    # ``run_id`` is the same one stamped into the Lance file above (#21).
    # Inline-await (NOT BackgroundTasks — no retry, dies with the worker; fastapi anti-pattern) so the event
    # reaches the durable Dapr/JetStream transport before the response. emit_create is best-effort internally,
    # so it never fails the create; JetStream + the consumer's idempotent MERGE-on-run_id give durability.
    # The per-version column schema (blob/vector-aware) for the WROTE edge (#24). A create/Overwrite writes
    # exactly the request's table, so the payload schema IS the table's schema — read in memory, no
    # describe + dataset reopen round trip. ExistOk is the exception: it may have KEPT an existing table
    # (nothing written, response.version = the existing version), so the payload schema could belong to a
    # table that was never created — read the true schema back PINNED at that version instead. Best-effort
    # either way (failure → []).
    if mode is CreateMode.EXIST_OK:
        _, schema_fields, _location = await run_in_threadpool(dataplane.read_version_and_schema, ns, so, segments, response.version)
    else:
        schema_fields = dataplane.payload_schema_fields(table.schema, segments)
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
        extra_run_facets=shape.run_facets,
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
