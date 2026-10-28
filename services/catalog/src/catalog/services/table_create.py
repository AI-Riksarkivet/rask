"""The GOVERNED create — every step of ``POST /v1/table/{id}/create`` except the routing.

Moved out of ``api/v1/endpoints/data.py`` (catalog-api-03), where it was fourteen awaits and fifty
statements inline in the handler against a module median of two. ``schemas.py``'s own header states the
intent for that plane — "the endpoints stay routing-only" — and this is the sequence that never left.

The steps, and the order, are exactly what the door ran, because the ORDER is the contract:

1. shape guards that cost nothing (wildcards, the multi-base allowlist, ``properties`` as a map of
   strings, the LANCE-ONLY format rule) — :func:`parse_create_shape` — and the body read as an Arrow IPC stream
   (``dataplane.read_arrow_body``). The door runs both before its idempotency claim, so a request that
   is invalid on its face never pays for a lookup (catalog-api-19) and never holds a key;
2. the parent-exists and live-trash guards — round trips, still strictly BEFORE the write;
3. the #21 lineage stamp into the decoded table's schema metadata;
4. the pre-existence probe, and for an Overwrite of an existing table its owner-tier drop gate and
   its deletion-protection guard;
5. the data-plane write, carrying the run the request's Context names in its transaction properties (the tier
   judge refused a missing run and an unreadable input before the door ran);
6. seed-with-compensation, which grants owner only on a table this request brought into being;
7. the schema read-back, the lineage emit (``overwrite_table`` for an Overwrite of an existing table)
   and, for a table this request brought into being, the control emit.

**An Overwrite of an existing table is a new version of the same table** ([[LH-242]]). The spec's
words are "the existing table is dropped and a new table with this name is created" (spec.yaml
CreateTableRequest.mode), and the backend writes a Lance overwrite on the same dataset, whose earlier
versions stay readable by time travel. The catalog takes the second meaning whole: the id keeps its
grants (revoking them while the history they guard stays readable gave the overwriter that history and
took it from everyone else), its provenance (``provenance_guard.keep_provenance``) and its protection
(a protected table refuses the Overwrite unless ``force=true``, as its drop does). The spec's "dropped"
survives as the owner-tier ``can_drop`` gate: the tip's schema and rows are replaced.

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
from functools import partial

import pyarrow as pa
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    CreateTableResponse,
    DescribeTableRequest,
    DropTableRequest,
    InvalidInputError,
    LanceNamespace,
    TableAlreadyExistsError,
    TableNotFoundError,
)
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel

from catalog.api import fga_deps
from catalog.api.dependencies import Resolver
from catalog.api.security import Caller
from catalog.core import encoding, provenance_guard, run_context
from catalog.core.base_judge import GovernedStorage
from catalog.core.config import Settings
from catalog.core.formats import reject_unsupported_properties
from catalog.core.identifiers import parse_identifier
from catalog.core.lineage_emit import CREATE_TABLE, OVERWRITE_TABLE, LineageEmitter, UuidReader, input_refs, run_inputs
from catalog.core.lineage_metadata import build_lineage_metadata, stamp_lineage_metadata
from catalog.core.modes import CreateMode
from catalog.core.run_context import RunContext
from catalog.services import dataplane, native, table_bases, table_claims, table_inputs
from service_kit.control_emit import ControlEmitter, emit_control
from service_kit.governed import fga
from service_kit.lakehouse import base_registry, data_directories, location_claims
from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)


def compensation_allowed(mode: CreateMode, overwrote_existing: bool) -> bool:
    """Whether a failed owner grant may compensate by DROPPING the table — only for a FRESH id.

    Never for ExistOk (it may have KEPT a pre-existing table this request never wrote) and never for
    an Overwrite that REPLACED an existing table (the id still holds the table's time-travel history;
    dropping would escalate a transient FGA blip into irreversible data loss — review 2026-07-10).
    Pure so the Overwrite arm is unit-testable (it needs FGA on, unreachable in the moto harness).
    """
    return mode is not CreateMode.EXIST_OK and not overwrote_existing


def table_exists(ns: LanceNamespace, segments: list[str]) -> bool:
    """True if a table already lives at ``segments`` (declared-only counts — it already holds an owner
    grant). Used to decide whether a create ``mode=Overwrite`` replaces an EXISTING table (which then
    needs the owner-tier gate and the protection guard, and is recorded as ``overwrite_table``) or
    creates a fresh one. Blocking native call → run in a threadpool."""
    try:
        native.call(ns, "describe_table", DescribeTableRequest(id=segments, check_declared=True))
        return True
    except TableNotFoundError:
        return False


class CreateShape(BaseModel):
    """What the create door decoded from the request itself, before its idempotency claim."""

    #: spec.yaml:3769-3775 types it as an object whose values are strings.
    properties: dict[str, str] | None = None
    #: The external blob base the create asks to register ([[LH-209]]), judged with settings before the write.
    external_blob_base: str | None = None


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
    external_blob_base: str | None = None,
) -> CreateShape:
    """Refuse a create that is malformed on its face, and return what it decoded.

    Pure: no round trip, no write. The door calls it BEFORE its idempotency claim, beside the mode
    parse, so a request refused here is answered from the request alone, with no claim to mint and
    give back on the store.
    """
    # #3-B governance (the security crux): validate BEFORE any write. An off-allowlist base is a client
    # error (400), never a silent write to an unapproved bucket.
    if data_base:
        approved = set(settings.multibase_data_base_list)
        rogue = [b for b in data_base if b not in approved]
        if rogue:
            raise InvalidInputError(f"data_base(s) not in the LANCE_MULTIBASE_DATA_BASES allowlist: {rogue}")
    parsed_properties = _parse_properties(properties)
    # #78 format honesty: reject a client that tries to select another file format (see the helper).
    reject_unsupported_properties(parsed_properties)
    # Create properties are stamped onto the Lance file's schema metadata, so they meet the same
    # reserved-namespace rule as every other metadata write door ([[LH-208]]).
    provenance_guard.refuse_reserved_keys(parsed_properties or {}, door="create properties")
    # Every later write encodes under the knobs a create stamps, so a value Lance cannot honour is refused
    # before the claim ([[LH-246]]).
    encoding.require_valid_encoding(parsed_properties)
    return CreateShape(properties=parsed_properties, external_blob_base=external_blob_base)


async def create_governed_table(
    *,
    id: str,
    ns: LanceNamespace,
    root: str,
    settings: Settings,
    token: Caller | None,
    client: OpenFgaClient | None,
    emitter: LineageEmitter,
    uuids: UuidReader,
    control: ControlEmitter,
    resolver: Resolver,
    so: StorageOptions,
    table: pa.Table,
    mode: CreateMode,
    shape: CreateShape,
    data_base: list[str],
    authorization: str | None,
    run: RunContext | None,
    force: bool = False,
) -> CreateTableResponse:
    """Create a Lance table from an Arrow-IPC stream, governed end to end.

    ``root`` is the root ``ns`` is rooted at, where a table this request brings into being is declared under its
    uuid (``dataplane.declare_table``, ADR 0173 §5).

    ``mode``, ``shape`` and ``table`` arrive PARSED ONCE, by the door, which refuses a malformed one
    before its idempotency claim (``CreateMode.parse``, :func:`parse_create_shape`,
    ``dataplane.read_arrow_body``); four decisions below turn on
    the mode — the pre-existence guards, the ownership seed, the schema read-back and the compensation
    rule. ``run`` is the run the request's Context names, which the tier judge already judged: it is stamped
    into the commit and its inputs become the event's inputs. ``force`` releases deletion protection for an Overwrite of an existing table,
    and nothing else.
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
    await fga_deps.require_parent_exists(ns, "table", parse_identifier(id))
    # [[LH-209]] The one external blob base this create may register, judged before the write: none unless
    # the request names one, and never one that reaches governed storage.
    external_base = await run_in_threadpool(
        table_bases.requested_external_blob_base,
        shape.external_blob_base,
        table.schema,
        approved=settings.external_blob_base_list,
        governed=GovernedStorage.from_settings(settings),
    )
    segments = parse_identifier(id)
    table_id = fga.canonical_object_id(segments)
    namespace = fga.parent_namespace_id(segments) or ""
    created_by = token.subject if token is not None else None
    # #21: stamp the lineage coordinates into the Lance file's schema metadata so the data is
    # self-describing (reconcilable to the graph without the catalog). A metadata swap on the decoded
    # table that shares its buffers, so no payload is too large to stamp. Gated on lineage being
    # enabled: when off we don't stamp coordinates the graph never receives.
    if settings.lineage_emit_enabled:
        table = stamp_lineage_metadata(table, build_lineage_metadata(namespace=namespace))
    # Pre-existence, computed BEFORE the write (declared-only counts — it already holds an owner grant).
    # Overwrite and ExistOk always probe, because what each did decides its lineage record, which does not
    # depend on FGA: an Overwrite of a table is not its create, and an ExistOk that kept one committed no rows
    # ([[LH-492]]). The probe's FGA questions:
    #   · Overwrite of an EXISTING table replaces the tip's schema and rows, the spec's "dropped" — so it
    #     clears the owner-tier can_drop a real drop needs, not only the writer-tier can_create_table the
    #     router applied on the parent, and a protected table refuses it unless force=true.
    #   · ExistOk that KEPT an existing table wrote NOTHING — so seeding the caller `owner` would let ANY
    #     authenticated user (or namespace-writer) SEIZE ownership of an already-owned table via a no-op
    #     create (audit: CRITICAL). We must never grant owner on a table this request did not create.
    probe = mode in (CreateMode.OVERWRITE, CreateMode.EXIST_OK)
    pre_existed = probe and await run_in_threadpool(table_exists, ns, segments)
    overwrote_existing = pre_existed and mode is CreateMode.OVERWRITE
    existok_kept_existing = pre_existed and mode is CreateMode.EXIST_OK
    overwriting: table_claims.DoorTable | None = None
    if overwrote_existing:
        overwritten = await resolver.table(segments, ns=ns)
        await fga_deps.require_can_drop_table(client, settings, token, table=overwritten)
        # After the authz gate, as on the drop door: a caller who may not drop the table learns nothing
        # about its protection.
        await fga_deps.require_table_unprotected(settings, overwritten, force=force)
        # The table asked about is the one overwritten ([[LH-530]]).
        overwriting = table_claims.DoorTable(ns, table_claims.claim_store(settings), overwritten)
    # [[LH-279]] Where the create records the bases it registers — the catalog's control root, written
    # with the catalog's own credential and never through a vend.
    registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options())
    # ``dataplane.create_table`` picks the write path by schema off the event loop: a blob-v2 column needs
    # file format 2.2 (native create pins 2.1 and rejects it) → a direct 2.2 write; else → native create. (§9)
    create = partial(
        dataplane.create_table,
        ns,
        so,
        segments,
        table,
        root=root,
        # An Overwrite of a name the probe found no table under is a create: one made under the name since is refused as
        # the create's own conflict, never overwritten without the `can_drop` asked above ([[LH-530]]).
        mode=mode if overwrote_existing or mode is not CreateMode.OVERWRITE else CreateMode.CREATE,
        properties=shape.properties,
        allow_external_blobs=settings.allow_external_blobs,
        external_blob_bases=[external_base] if external_base else [],
        data_bases=data_base or None,
        registry=registry,
        holder=table_id,
        transaction_properties=run_context.commit_properties(run, author=created_by, operation=OVERWRITE_TABLE if overwrote_existing else CREATE_TABLE),
        overwriting=overwriting,
        before_commit=table_inputs.recorder(
            settings.registry_root, settings.storage_options(), name=table_id, declared=await fga_deps.declared_inputs(resolver, run)
        ),
    )
    try:
        response: CreateTableResponse = await run_in_threadpool(create)
    except (TableAlreadyExistsError, ConcurrentModificationError):
        # AN ExistOk THAT LOST THE RACE KEEPS THE WINNER'S TABLE. Two ExistOk creates in one pod both find no table at
        # the probe, and the backend's put-if-not-exists answers the loser's add as a conflict; ExistOk asks for the
        # table to exist, so the loser answers it as kept, and as kept it writes nothing and seeds no owner. A winner
        # still between its declaration and its first write holds no version yet, and the conflict stands: a retry
        # finds the table written.
        if mode is not CreateMode.EXIST_OK or (kept := await run_in_threadpool(dataplane.kept_table, ns, so, segments)) is None:
            raise
        response, existok_kept_existing = kept, True

    # Make the caller owner of a table this request brought into being + link it to its parent so it
    # inherits the cascade. An Overwrite of an existing table keeps every grant the table holds, the owner's
    # included, and grants the overwriter nothing: it already cleared can_drop, and the table is not new.
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
    # table wrote nothing, and an Overwrite of one wrote a version of a table someone else brought into being,
    # so granting the caller `owner` on either would seize it (audit: CRITICAL) — the existing owner (or the
    # /declare-r of a declared-only table) keeps ownership. Skipping the seed also
    # skips the compensation (there is nothing this request wrote to compensate).
    async def _undo_create() -> None:
        await run_in_threadpool(native.call, ns, "drop_table", DropTableRequest(id=segments))
        # The bytes are gone with the table, so its base record goes too: a record that outlives the
        # manifest it describes would vouch for bases nothing declares. The fragments the write put in the table's data
        # directories go before it ([[LH-286]]), and its location's claim goes with it.
        if response.location:
            await run_in_threadpool(
                partial(data_directories.end_base_record, registry, response.location, table=table_id, storage_options=settings.storage_options())
            )
            await run_in_threadpool(location_claims.release, table_claims.claim_store(settings), response.location, table_id)

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
    seeding_a_new_table = not pre_existed and not existok_kept_existing
    # The table's grants are held by the uuid its location's claim records: the one this create minted, or the kept
    # table's own under ExistOk or an Overwrite (ADR 0173 §2, [[LH-524]]).
    table_uuid = await run_in_threadpool(table_claims.uuid_at, table_claims.claim_store(settings), response.location or "", segments)
    await fga_deps.seed_ownership_or_compensate(
        client,
        settings,
        token,
        resolver,
        resource="table",
        segments=segments,
        uuid=table_uuid,
        may_grant_owner=seeding_a_new_table,
        undo=_undo_create if (seeding_a_new_table and compensation_allowed(mode, overwrote_existing)) else None,
    )
    # Record provenance authoritatively: the catalog knows the verified principal. Fire-and-forget
    # (after the response, best-effort) so the lineage service can never block/fail a create. The
    # canonical id names the lineage Dataset as the catalog names the table, and the event carries the table's uuid,
    # which its OpenFGA object names (ADR 0173 §8, §10); the caller's bearer is forwarded so ingest accepts it when the lineage service has OIDC on; the
    # run id is derived from the commit (`catalog.core.commit_ids`).
    # Inline-await (NOT BackgroundTasks — no retry, dies with the worker; fastapi anti-pattern) so the event
    # reaches the durable Dapr/JetStream transport before the response. emit_create is best-effort internally,
    # so it never fails the create; JetStream + the consumer's idempotent MERGE-on-run_id give durability.
    # The per-version column schema (blob/vector-aware) for the WROTE edge (#24). A fresh create writes
    # exactly the request's table, so the payload schema IS the table's schema. Two exceptions take the true
    # schema read back PINNED at the version: ExistOk may have KEPT an existing table (nothing written,
    # response.version = the existing version), and an Overwrite of an existing table wrote the table's
    # provenance fields and metadata over the payload's. The read-back is made for every create, for the
    # commit's own time, the event's `eventTime` ([[LH-497]]). Best-effort either way (failure → []).
    readback = await run_in_threadpool(dataplane.read_version_and_schema, str(response.location), so, segments, response.version, None)
    schema_fields = readback.schema_fields if mode is CreateMode.EXIST_OK or overwrote_existing else dataplane.payload_schema_fields(table.schema, segments)
    inputs = await input_refs(run_inputs(run), uuids=uuids)
    # Read through the location the write landed in, so a rename since cannot lend the event another table's uuid.
    table_uuids = await uuids.table(segments, location=response.location)
    if overwrote_existing:
        # Not a creation: the table, its creator and its CREATED edge all predate this request.
        await emitter.emit_write(
            table_id=table_id,
            namespace=namespace,
            author=created_by,
            version=response.version,
            operation=OVERWRITE_TABLE,
            authorization=authorization,
            source_uri=response.location,
            schema_fields=schema_fields,
            inputs=inputs,
            run=run,
            uuids=table_uuids,
            event_time=readback.committed_at,
        )
    else:
        await emitter.emit_create(
            table_id=table_id,
            namespace=namespace,
            author=created_by,
            version=response.version or 1,
            authorization=authorization,
            source_uri=response.location,  # the real Lance URI → #23 reconcile can read the on-disk file
            schema_fields=schema_fields,
            inputs=inputs,
            run=run,
            # The payload's rows landed at v1 only when this request wrote the table.
            commits_rows=table.num_rows > 0 and not existok_kept_existing,
            uuids=table_uuids,
            event_time=readback.committed_at,
        )
    # Only a table this request brought into being is announced on the control lane. An ExistOk request that
    # KEPT a pre-existing table created nothing, and an Overwrite of an existing table kept its uuid, grants and
    # protection: it committed a new version of the same dataset (lance_docs transaction.md § Overwrite), so its
    # announcement is the `overwrite_table` lineage event above, as `insert?mode=overwrite` and a staged Overwrite
    # at `version/create` announce theirs (ADR 0099, [[LH-415]]).
    if not existok_kept_existing and not overwrote_existing:
        await emit_control(
            control,
            action="table_created",
            object_type="table",
            object_id=fga.table_object(table_uuid),
            object_name=table_id,
            actor=f"user:{token.subject}" if token is not None else None,
            extra={
                "namespace": namespace,
                "version": response.version or 1,
                "mode": mode,
                "location": response.location,
            },
        )
    return response
