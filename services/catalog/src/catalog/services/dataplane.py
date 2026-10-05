"""Table data, schema and tag operations implemented in-process via pylance.

The native ``DirectoryNamespace`` stubs several table data, schema, and tag operations. These
functions fill the gap: resolve the table's dataset via the namespace, then perform the operation
with pylance.

Most take ``(ns, storage_options, request)`` and return the typed ``lance_namespace`` response model.
Exceptions: ``update_field_metadata`` takes ``(ns, storage_options, table_id, updates)``; and
``create_table`` is the one facade that receives the raw Arrow-IPC ``data`` and writes it directly at
file format 2.2 with stable row ids — the durable row identity row-id lineage needs.

Scope: pylance dataset semantics only. HTTP read semantics over a blob payload — byte ranges,
``If-Range``, ETags, satisfiability, the bounded streaming window — belong to ``blob_serving.py``,
which changes for RFC 9110 reasons rather than pylance ones. Nothing here shapes a response.
"""

from __future__ import annotations

import io
import itertools
import json
import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from functools import partial
from typing import Any, Final, cast

import lance
import lance.optimize as lance_optimize
import pyarrow as pa
import pyarrow.fs as pafs
from lance_namespace import (
    AlterTableAddColumnsRequest,
    AlterTableAddColumnsResponse,
    AlterTableAlterColumnsRequest,
    AlterTableAlterColumnsResponse,
    AlterTableDropColumnsRequest,
    AlterTableDropColumnsResponse,
    ConcurrentModificationError,
    CountTableRowsRequest,
    CountTableRowsResponse,
    CreateTableBranchRequest,
    CreateTableBranchResponse,
    CreateTableIndexRequest,
    CreateTableResponse,
    CreateTableTagRequest,
    CreateTableTagResponse,
    DeclareTableRequest,
    DeleteFromTableRequest,
    DeleteFromTableResponse,
    DeleteTableBranchRequest,
    DeleteTableBranchResponse,
    DeleteTableTagRequest,
    DeleteTableTagResponse,
    DeregisterTableRequest,
    DescribeTableRequest,
    DescribeTransactionRequest,
    DropTableRequest,
    GetTableTagVersionRequest,
    GetTableTagVersionResponse,
    InsertIntoTableRequest,
    InsertIntoTableResponse,
    InvalidInputError,
    InvalidTableStateError,
    LanceNamespace,
    LanceNamespaceError,
    ListTableBranchesRequest,
    ListTableBranchesResponse,
    ListTableIndicesRequest,
    ListTableTagsRequest,
    ListTableTagsResponse,
    MergeInsertIntoTableRequest,
    MergeInsertIntoTableResponse,
    RegisterTableRequest,
    ServiceUnavailableError,
    TableAlreadyExistsError,
    TableBranchAlreadyExistsError,
    TableBranchNotFoundError,
    TableColumnNotFoundError,
    TableNotFoundError,
    TableSchemaValidationError,
    TableTagAlreadyExistsError,
    TableTagNotFoundError,
    TableVersionNotFoundError,
    UnsupportedOperationError,
    UpdateFieldMetadataResponse,
    UpdateTableRequest,
    UpdateTableResponse,
    UpdateTableTagRequest,
    UpdateTableTagResponse,
)
from pydantic import BaseModel

from catalog.core import provenance_guard
from catalog.core.base_judge import require_sanctioned_bases
from catalog.core.config import shared_lance_session
from catalog.core.modes import CreateMode, InsertMode
from catalog.core.namespace import judged_native_version, open_dataset, open_dataset_unchecked
from catalog.services import changes, client_fragments, native, table_bases, table_claims, warehouse_credentials
from catalog.services.base_credentials import compose_base_store_params
from catalog.services.cast_size import bytes_after_cast
from service_kit.lakehouse import base_registry, branch_layout, commit_runs, location_claims
from service_kit.lakehouse.base_refs import decoded_path
from service_kit.lakehouse.features import VersionedDataFile, describe_foreign_data_file_versions, manifest_base_path_refs
from service_kit.lakehouse.objectfs import StorageOptions, credential_of, s3_filesystem
from service_kit.lakehouse.schema import SchemaFields, facet_fields
from service_kit.lancekit.absence import reads_as_absent
from service_kit.lancekit.arrow_ipc import ArrowBodyError, ArrowBodyTooLargeError, decode_arrow_stream, encode_arrow_stream
from service_kit.lancekit.commit_verdict import CommitVerdict, classify_commit_failure
from service_kit.lancekit.versions import committed_at
from storage import s3_client, split_s3_uri


log = logging.getLogger(__name__)


def _table_id(req: object) -> list[str]:
    """Return the request's table identifier, or raise if it is empty."""
    table_id = getattr(req, "id", None)
    if not table_id:
        raise InvalidInputError("table identifier is required")
    return table_id


def read_version_and_schema(
    ns: LanceNamespace,
    so: StorageOptions,
    table_id: list[str],
    pin_version: int | None,
    branch: str | None,
) -> tuple[int | None, SchemaFields, str | None]:
    """The ``(version, schema-facet fields, location)`` triple for stamping lineage after a committed write.

    ``pin_version`` is the version the write COMMITTED, as the write itself reported it: a handle's
    ``version`` after the commit, a response's ``version``, or :func:`committed_version` for a response
    carrying only a ``transaction_id``. ONE dataset open, AT that version, serves the schema and the
    location, so neither can come from another writer's snapshot. ``None`` means the commit could not be
    identified, and the answer is then versionless and schemaless: the latest snapshot names whichever
    commit landed last, which under concurrent appends is somebody else's (measured on pylance 12.0.0:
    two handles appending in turn commit v3 and v4, and a reopen after both reports v4 for each).

    ``branch`` must name the ref the write COMMITTED TO: a branch keeps its own version sequence, so
    version N opened on main is a different snapshot.

    Entirely best-effort: the write is already committed, so a readback failure must degrade the lineage
    enrichment (``(pin_version, [], None)``), never fail the request.
    """
    try:
        dataset = open_dataset(ns, so, table_id, version=pin_version, branch=branch)
        # The physical URI rides the SAME handle, so the standard `dataSource` facet costs nothing
        # beyond this open. Without it #23 reconcile has no URI to read and reports every live table
        # `missing_on_storage`, and an event-driven consumer receives a table id it cannot resolve —
        # `services/maintenance` holds no catalog client by design.
        location = str(getattr(dataset, "uri", "") or "") or None
    except Exception as exc:
        log.warning("lineage_readback_failed", extra={"table": table_id, "error": str(exc)})
        return pin_version, [], None
    if pin_version is None:
        return None, [], location
    try:
        return pin_version, facet_fields(dataset.schema), location
    except Exception as exc:
        log.warning("schema_facet_read_failed", extra={"table": table_id, "error": str(exc)})
        return pin_version, [], location


#: How far below a branch's head :func:`committed_version` looks for a transaction. The commit being
#: identified has just landed, so it sits at the head unless other writers committed after it; the bound
#: keeps a lookup on a busy branch from walking its whole history.
_TRANSACTION_SEARCH_DEPTH: Final = 64


def committed_version(ns: LanceNamespace, so: StorageOptions, table_id: list[str], transaction_id: str | None, *, branch: str | None) -> int | None:
    """The version the transaction ``transaction_id`` committed on ``branch``, or ``None`` when it cannot be found.

    For the native ops whose response carries a ``transaction_id`` and no version (restore, schema
    metadata, the index doors): the spec's ``DescribeTransaction`` answers ``properties.version``
    (``spec.yaml`` ``/v1/transaction/{id}/describe``), the version that transaction made rather than the
    one the table is at now. Lakekeeper builds its commit event from its own transaction's context the
    same way, never from a re-read.

    A BRANCH IS SEARCHED ON ITS OWN HANDLE, because the ``dir`` backend's ``describe_transaction`` reads
    main's history only: measured on pylance 12.0.0, a restore on branch ``x`` answered a transaction id
    that ``describe_transaction`` reports ``TransactionNotFound``, while ``read_transaction`` on the
    branch's head returned that uuid. The walk starts at the head and stops at the bound or above the
    branch point, the first version the branch did not commit.

    Best-effort: ``None`` makes the event versionless, never the request a failure.
    """
    if not transaction_id:
        return None
    try:
        if recorded_branch(branch) is None:
            described = ns.describe_transaction(DescribeTransactionRequest(id=[*table_id, transaction_id]))
            raw = (described.properties or {}).get("version")
            return int(raw) if raw is not None else None
        dataset = open_dataset_unchecked(ns, so, table_id, branch=branch)
        head = int(dataset.version)
        # THE WALK STOPS ABOVE THE BRANCH POINT. A branch's first manifest carries the parent's version
        # number and no transaction a write could have made, and on pylance 12.0.0 `read_transaction` on
        # it PANICS (`pyo3_runtime.PanicException: not yet implemented`) while one version lower raises
        # OSError (no manifest of the branch's own). Nothing at or below `parent_version` is this
        # branch's commit, so neither is read.
        listed = dataset.branches.list().get(cast(str, recorded_branch(branch))) or {}
        parent = listed.get("parent_version")
        floor = max(head - _TRANSACTION_SEARCH_DEPTH, int(parent) if isinstance(parent, int) else 0)
        for version in range(head, floor, -1):
            transaction = dataset.read_transaction(version)
            if transaction is not None and transaction.uuid == transaction_id:
                return version
    except BaseException as exc:  # noqa: BLE001 — a Rust PANIC is not an Exception; see below
        # `BaseException`, the way `maintenance.compact_now` catches it: a pyo3 panic derives from
        # BaseException and cannot be caught by name (`pyo3_runtime` is synthesised lazily and is not
        # importable). This runs after the write committed, so an escaped panic answers 500 for a commit
        # that happened and skips the door's idempotency record, and a retry commits again.
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        log.warning("committed_version_unreadable", extra={"table": table_id, "branch": branch, "error": str(exc), "error_type": type(exc).__name__})
    return None


def branch_identifier(ns: LanceNamespace, so: StorageOptions, table_id: list[str], branch: str | None) -> str | None:
    """The identifier of the CURRENT incarnation of ``branch``, or ``None`` for main or when unreadable.

    pylance 12's ``branches.list()`` carries ``branch_identifier``, the version mapping stored in
    ``_refs/branches/<b>.json``: a list of ``(parent version, uuid)`` pairs from the first branch off main
    down to this one, whose LAST uuid is minted when this branch is created. Measured on 12.0.0: it
    differs across a delete-and-recreate within the same second while ``parentVersion`` and ``createAt``
    are identical, and a nested branch extends its parent's list by one pair.

    Best-effort, like the rest of the lineage enrichment it feeds.
    """
    name = recorded_branch(branch)
    if name is None:
        return None
    try:
        entry = open_dataset_unchecked(ns, so, table_id).branches.list().get(name)
    except Exception as exc:
        log.warning("branch_identifier_unreadable", extra={"table": table_id, "branch": name, "error": str(exc)})
        return None
    return identifier_of(entry)


#: The key a branch's identifier is published under: in ``list_branches``' per-branch ``metadata`` and in
#: the ``extra`` of the branch control events. pylance's own field name, so it reads the same everywhere.
BRANCH_IDENTIFIER_KEY: Final = "branch_identifier"


def identifier_of(entry: object) -> str | None:
    """The last uuid of a listed branch's ``branch_identifier`` chain: the one that names this incarnation."""
    chain = entry.get("branch_identifier") if isinstance(entry, dict) else None
    if not isinstance(chain, list) or not chain:
        return None
    last = chain[-1]
    return str(last[1]) if isinstance(last, tuple | list) and len(last) == 2 else None


def payload_schema_fields(schema: pa.Schema, table_id: list[str]) -> SchemaFields:
    """Schema-facet fields read off the create payload's schema (no storage round trip).

    A create/Overwrite writes exactly this payload, so its schema — including the blob/vector field
    metadata ``facet_fields`` keys on — IS the new table's schema; re-opening the just-written dataset
    would cost a describe + object-store open for information already in memory. NOT valid for ExistOk
    (which may keep an existing table the payload never touched) — that path reads back pinned instead.
    ``table_id`` is logging context only. Best-effort (``[]`` on a failure): the schema facet is an
    enrichment, never a reason to fail the write.
    """
    try:
        return facet_fields(schema)
    except Exception as exc:
        log.warning("schema_facet_parse_failed", extra={"table": table_id, "error": str(exc)})
        return []


def _base_name(uri: str) -> str:
    """A stable, deterministic base NAME for a #3-B data-distribution base URI.

    ``target_bases`` references bases by name; deriving the name from the URI (not a random id) means a create
    and any later op agree byte-for-byte, and the manifest's registered-base names stay human-readable."""
    return uri.replace("s3://", "").strip("/").replace("/", "-") or "base"


#: Lance's per-field encoding knobs (`lance_docs/file_format.md` — Compression Configuration). A create
#: carries them as ordinary table properties; they reach Lance as FIELD metadata on the written schema.
_ENCODING_PREFIX = "lance-encoding:"


def _is_variable_width(dtype: pa.DataType) -> bool:
    """The types general compression actually acts on — the ones Lance stores through its `Variable`
    compressor. Bitpacking (ints) and BSS (floats) are chosen by Lance from the data itself."""
    return bool(pa.types.is_string(dtype) or pa.types.is_large_string(dtype) or pa.types.is_binary(dtype) or pa.types.is_large_binary(dtype))


def _apply_encoding(table: pa.Table, properties: dict[str, str] | None) -> pa.Table:
    """Stamp any ``lance-encoding:*`` create property onto the VARIABLE-WIDTH fields of the schema.

    **NOTHING IS SET BY DEFAULT, and that is a measured decision rather than an omission** — recorded in
    `docs/DECISIONS.md`. General compression runs AFTER FSST/bitpacking/RLE, so on small values it adds a
    frame per block and buys nothing: measured on this estate's own tier shape it COSTS up to +78% at
    256 B values and only starts saving above ~1 KiB. The tier payload is opaque by design
    (`medallion/schemas/tier.py`), so no one scheme can be right for every table — a workload that knows
    its own value sizes opts in, per table, through this property.

    VARIABLE-WIDTH ONLY, deliberately: ``lance-encoding:bss`` engages byte-stream-split on floats only
    where general compression is also applied, so stamping every field would quietly change float
    encoding as a side effect of asking for string compression.
    """
    encoding = {k: v for k, v in (properties or {}).items() if k.startswith(_ENCODING_PREFIX)}
    if not encoding:
        return table
    schema = pa.schema(
        [f.with_metadata({**(f.metadata or {}), **encoding}) if _is_variable_width(f.type) else f for f in table.schema],
        metadata=table.schema.metadata,
    )
    return pa.Table.from_arrays(table.columns, schema=schema)


def _write_blob(
    table: pa.Table,
    uri: str,
    so: StorageOptions,
    *,
    mode: str,
    allow_external: bool,
    external_blob_bases: list[str],
    data_bases: list[str] | None = None,
    properties: dict[str, str] | None = None,
    base_credential_refs: dict[str, str] | None = None,
    secret_store: str = "",
    secret_field: str = "",
) -> lance.LanceDataset:
    """Write a table at file format 2.2 with stable row ids.

    ``allow_external`` opts into ``Blob.from_uri`` columns ANYWHERE outside the dataset root (blanket bypass);
    ``external_blob_bases`` registers approved base URIs so external pointers UNDER a registered base are
    accepted with the bypass left off — the safer allowlist posture (lance_docs/guide.md). ``data_bases``
    (#3-B) are approved DATA-distribution bases the fragments round-robin across (the Uber pattern).
    Bases register on a fresh CREATE; an overwrite reuses the bases the table registered at create."""
    is_create = mode == "create"
    table = _apply_encoding(table, properties)
    # De-dup: a repeated data_base must not double-register / double-target the round-robin.
    data_bases = list(dict.fromkeys(data_bases or []))
    # _base_name is lossy (s3://b/a/c and s3://b/a-c both → b-a-c). A collision would silently make one
    # approved base unaddressable + make target resolution ambiguous — reject it loudly, never misroute.
    data_names = [_base_name(u) for u in data_bases]
    if len(set(data_names)) != len(data_names):
        raise InvalidInputError(f"data_base paths collide on base name {data_names}; use distinct base paths")
    # DatasetBasePath registers each approved base (is_dataset_root=False = a raw data location, not a nested
    # dataset). initial_bases REGISTERS the bases in the manifest — CREATE-only (an overwrite/append reuses
    # the already-registered set; re-registering on overwrite is rejected by pylance).
    external_paths = [lance.DatasetBasePath(b, is_dataset_root=False) for b in external_blob_bases]
    data_paths = [lance.DatasetBasePath(u, is_dataset_root=False, name=n) for u, n in zip(data_bases, data_names, strict=True)]
    _has_bases = bool(external_blob_bases or data_bases)
    initial_bases = (external_paths + data_paths) if is_create and _has_bases else None
    # target_bases is the WRITE TARGET: it round-robins the FRAGMENT writes across the data bases while the
    # manifest + _versions stay in the primary root (relative-path portable — a relocation moves the base
    # URIs, not 10M file paths). Applied on ANY mode when data_bases is SUPPLIED, so a re-supplied overwrite
    # ALSO distributes (the names must match the manifest's registered bases — deterministic _base_name makes
    # a re-sent same list match). CAVEAT: a mutation that does NOT re-send data_base (a bare overwrite, or the
    # /insert append route which has no data_base param) concentrates its NEW fragments in the primary root —
    # create-time distribution is the firm guarantee; per-write distribution needs the bases re-supplied.
    target_bases = data_names or None
    # base_store_params: each base's object-store options at RUNTIME (pylance does NOT persist these to
    # the manifest — verified against the write_dataset/dataset docstrings, which also state they take
    # precedence over `base_<id>.<key>` in storage_options; that non-persistence is what makes this the
    # only form a CREDENTIAL may take here).
    #
    # PER BASE NOW, NOT ONE DICT FOR ALL ([[LH-067]]). A base with a configured credential REFERENCE
    # gets its own entry, resolved through the Dapr secret store; a base without one is OMITTED, and
    # pylance's documented fallback ("when a base has no explicit entry here, the top-level
    # storage_options is used") makes that byte-identical to sending the estate options explicitly. So
    # an estate configuring no references renders `{}` and behaves exactly as before.
    #
    # The READ path forwards these too now (`core/namespace.open_dataset`), which is what retires the
    # operator obligation this comment used to carry: a base needing different credentials no longer
    # "writes OK but is unreadable".
    base_store_params = (
        (
            compose_base_store_params(
                bases=data_bases,
                storage_options=so,
                refs=base_credential_refs or {},
                resolve=warehouse_credentials.resolve,
                store=secret_store,
                field=secret_field,
            )
            or None
        )
        if data_bases
        else None
    )
    try:
        return lance.write_dataset(  # noqa: TID251
            table,
            uri,
            mode=mode,
            storage_options=so,
            data_storage_version="2.2",
            # #5a: durable row identity — ``_rowid`` stays constant across compaction (which rewrites
            # fragments and invalidates row ADDRESSES), gating the row-id index + row-version tracking
            # (``_row_*_at_version``) that field-level/temporal lineage rests on. CREATE-TIME-ONLY (silently
            # no-ops on an existing dataset), so it's set on every create/overwrite here — matching the
            # medallion cascade writes (compute.py).
            enable_stable_row_ids=True,
            initial_bases=initial_bases,
            target_bases=target_bases,
            base_store_params=base_store_params,
            allow_external_blob_outside_bases=allow_external,
        )
    except OSError as exc:
        # An external-pointer blob outside every registered base (and the blanket flag off) is a client
        # error, not a 500 — surface a clear 400. Match lance's specific phrase (NOT a bare "external"), so a
        # genuine infra OSError on a path that merely contains the word "external" still surfaces as a 500.
        if not allow_external and "outside registered external bases" in str(exc).lower():
            raise InvalidInputError(
                "blob column references an external object outside the dataset root and any registered "
                "external base; configure LANCE_EXTERNAL_BLOB_BASES with the approved base(s), or "
                "LANCE_ALLOW_EXTERNAL_BLOBS for the blanket bypass, to accept Blob.from_uri columns"
            ) from exc
        # A CREATE THAT LANDS ON OCCUPIED BYTES IS A CONFLICT, NOT AN INTERNAL FAILURE
        # ([[LH-164]]). `spec.yaml:1461` declares `ConflictErrorResponse` on `CreateTable`, so a
        # collision is a modelled outcome of this operation. Measured on the live catalog 2026-09-21:
        # `POST /v1/table/silver$features/create` answered 500 `detail: "Internal Server Error"` while
        # the reason — `Dataset already exists: s3://bind86-wh/medallion/silver` — existed only in the
        # pod's traceback.
        #
        # THE DETAIL NAMES THE LOCATION AND DENIES THE TABLE, because those bytes are ungoverned
        # residue at the path the catalog COMPOSES for this id, not a table record. "Table already
        # exists" would send the caller looking for something that does not exist; the location is the
        # one actionable fact, and it is exactly what the 500 withheld.
        #
        # Matches lance's specific phrase for the same reason the branch above does: a translation
        # that swallowed every OSError would relabel genuine infra failures as client errors.
        if "dataset already exists" in str(exc).lower():
            raise TableAlreadyExistsError(
                f"a dataset already occupies {uri}, and the catalog holds no table record for it — "
                "so it is storage residue rather than a governed table. Clear or relocate that "
                "location, or create under an id that composes to a different one"
            ) from exc
        raise


def read_arrow_body(data: bytes, *, max_bytes: int) -> pa.Table:
    """A write body as a table, or `InvalidInputError` (400, code 13) saying it is too large or not a valid Arrow IPC stream.

    Decoded by the fleet's one validating decoder (`service_kit.lancekit.arrow_ipc`): read whole, then
    validated in full, because framing that parses says nothing about the buffers — Lance writes an
    offset past its values buffer as bytes from outside the request (pyarrow 25.0.0, pylance 12.0.0).
    ``max_bytes`` is the catalog's body cap (``LANCE_MAX_BODY_BYTES``), which the body may not exceed
    once its compressed buffers inflate: the write load-shed sizes its concurrency as if each write held
    at most that much (``values-prod.yaml``, ``catalog.maxConcurrentWrites``), and the native backend
    inflates the same bytes again on the main arm. :func:`coerce_insert_arrow` holds the rows it
    re-encodes to the same cap.
    """
    try:
        return decode_arrow_stream(data, max_bytes=max_bytes)
    except ArrowBodyTooLargeError as exc:
        raise InvalidInputError(
            f"the request body is too large once decoded: {exc}; write large payloads directly to object storage with vended credentials"
            " instead of through the catalog"
        ) from exc
    except ArrowBodyError as exc:
        raise InvalidInputError(f"the request body is not a valid Arrow IPC stream: {exc}") from exc


def create_table(
    ns: LanceNamespace,
    so: StorageOptions,
    segments: list[str],
    table: pa.Table,
    *,
    mode: str | CreateMode | None = None,
    properties: dict[str, str] | None = None,
    allow_external_blobs: bool = False,
    external_blob_bases: list[str] | None = None,
    data_bases: list[str] | None = None,
    base_credential_refs: dict[str, str] | None = None,
    secret_store: str = "",
    secret_field: str = "",
    registry: base_registry.BaseRegistry | None,
) -> CreateTableResponse:
    """Create a table at file format 2.2 with stable row ids — the ONLY create path (audit 2026-07-14).

    Serves EVERY create, and IS the door: this used to sit behind a same-named 36-line wrapper that
    forwarded all nine arguments and renamed two on the way (CAT-CORE-17). 2.2 + stable row ids are
    create-time-only, so a table that skipped this path could never gain the durable row identity
    ``row_id_lineage`` needs (row-level provenance: model → dataset version → the exact source rows).

    ``table`` arrives decoded: the door reads the body with :func:`read_arrow_body` before its
    idempotency claim, so a body that is not an Arrow stream is refused 400 with nothing written.

    Runs off the event loop (blocking Lance/S3 IO), so the endpoint stays a single delegated call.
    ``allow_external_blobs`` permits ``Blob.from_uri`` columns pointing ANYWHERE outside
    the dataset root (the blanket bypass); ``external_blob_bases`` is the safer allowlist — external
    pointers are accepted only under one of these registered bases, with the blanket bypass left off.
    ``data_bases`` (#3-B) spreads the table's fragments across N approved buckets (Lance multi-base);
    empty (the default) → a single-base write.

    Honours the create ``mode`` against a *written* table (``ExistOk`` keeps it, ``Overwrite`` writes a new
    version of it that keeps the table's provenance — :func:`provenance_guard.keep_provenance` — while its
    history stays readable, ``Create`` conflicts). A *declared-only* table — one that exists in the namespace but was never
    written (a bare ``POST /declare``, or a declare→write that crashed before the write) — has no readable
    dataset, so every mode simply lands the first data version into its already-declared location; this keeps
    the multi-step create idempotent and crash-safe, and never opens a table that isn't there (which 500'd).
    A brand-new table is ``declare``-d to learn its canonical location, then written at
    ``data_storage_version="2.2"`` (lance_docs/guide.md — Version Compatibility). Any failed fresh write is
    rolled back with ``drop_table`` so the name stays retryable rather than stuck describable-but-unreadable.

    ``registry`` is where a fresh write records the bases it registers ([[LH-279]]): the record is the
    catalog's word on which foreign bases the table may declare, written BEFORE the manifest and removed
    again when the write fails. Required and never defaulted — the governed door passes the control root;
    ``None`` is an explicit choice for a caller outside governance (a test building a fixture), which
    leaves the table with no record at all.
    """
    allow_external = allow_external_blobs
    external_blob_bases = external_blob_bases or []
    normalized = CreateMode.parse(mode)
    existing, only_declared = _existing_location(ns, segments)

    if existing is not None and not only_declared:  # a written, readable table already lives here
        if normalized is CreateMode.OVERWRITE:
            # A new version of the same table, so the table's provenance rides onto it ([[LH-242]]).
            current = lance.dataset(existing, storage_options=so, session=shared_lance_session()).schema
            dataset = _write_blob(
                provenance_guard.keep_provenance(current, table),
                existing,
                so,
                mode="overwrite",
                allow_external=allow_external,
                external_blob_bases=external_blob_bases,
                data_bases=data_bases,
                properties=properties,
                base_credential_refs=base_credential_refs,
                secret_store=secret_store,
                secret_field=secret_field,
            )
            return CreateTableResponse(location=existing, version=dataset.version, properties=properties)
        if normalized is CreateMode.EXIST_OK:  # keep it untouched, just report its current version
            version = lance.dataset(existing, storage_options=so, session=shared_lance_session()).version
            return CreateTableResponse(location=existing, version=version, properties=properties)
        # `create` against a written table → let declare surface the canonical TableAlreadyExists conflict.

    if existing is not None and only_declared:  # declared, no data yet → write into it (all modes)
        return _write_blob_into(
            ns,
            table,
            existing,
            so,
            segments,
            properties,
            allow_external=allow_external,
            external_blob_bases=external_blob_bases,
            data_bases=data_bases,
            base_credential_refs=base_credential_refs,
            secret_store=secret_store,
            secret_field=secret_field,
            registry=registry,
        )

    location = ns.declare_table(DeclareTableRequest(id=segments, properties=properties)).location
    if not location:
        raise InvalidInputError("namespace did not return a location for the declared table")
    return _write_blob_into(
        ns,
        table,
        location,
        so,
        segments,
        properties,
        allow_external=allow_external,
        external_blob_bases=external_blob_bases,
        data_bases=data_bases,
        base_credential_refs=base_credential_refs,
        secret_store=secret_store,
        secret_field=secret_field,
        registry=registry,
    )


def _write_blob_into(
    ns: LanceNamespace,
    table: pa.Table,
    location: str,
    so: StorageOptions,
    segments: list[str],
    properties: dict[str, str] | None,
    *,
    allow_external: bool,
    external_blob_bases: list[str],
    data_bases: list[str] | None = None,
    base_credential_refs: dict[str, str] | None = None,
    secret_store: str = "",
    secret_field: str = "",
    registry: base_registry.BaseRegistry | None,
) -> CreateTableResponse:
    """Write the blob table's first data version into an already-declared ``location``, rolling the declare
    back with ``drop_table`` on failure so the name stays retryable rather than stuck declared-but-unreadable.

    THE BASE RECORD IS CLAIMED FIRST ([[LH-279]]): every base this write registers through
    ``initial_bases`` — the external blob base the create asked for and the approved data bases — is in the
    catalog's record before the manifest that declares them exists, so no reader ever meets the table
    declaring a base its record lacks. A failed write releases exactly what it claimed; a record that
    outlived its write would vouch for a base no manifest names.
    """
    claim: base_registry.BaseClaim | None = None
    try:
        if registry is not None:
            entries = table_bases.create_entries(external_blob_bases, [(base, _base_name(base)) for base in dict.fromkeys(data_bases or [])])
            claim = base_registry.claim_bases(registry, location, entries)
        dataset = _write_blob(
            table,
            location,
            so,
            mode="create",
            allow_external=allow_external,
            external_blob_bases=external_blob_bases,
            data_bases=data_bases,
            properties=properties,
            base_credential_refs=base_credential_refs,
            secret_store=secret_store,
            secret_field=secret_field,
        )
    except Exception:
        with suppress(Exception):  # best-effort rollback; re-raise the real write error
            ns.drop_table(DropTableRequest(id=segments))
        if registry is not None and claim is not None:
            try:
                base_registry.release_claim(registry, claim)
            except Exception as release_exc:  # noqa: BLE001 — the write error is what the caller is told
                log.error("create_base_record_release_failed", extra={"location": location, "error": str(release_exc)[:300]})
        raise
    if properties:
        # STAMPED ON THE TABLE, not only on the manifest row and the reply. The spec's "properties at
        # create" was true of `declare_table` and of the response echo and false of the Lance file, so
        # a client that created with `{"owner": …}` and then asked the TABLE got nothing back and no
        # error saying why — the one shape a test on the response body can never catch.
        #
        # MERGE, never `replace=True`, for the reason `update_schema_metadata` records: a replace drops
        # the internal `lineage.*` coordinates that make the file self-describing. Nothing here can
        # have written them yet, but the rule is the seam's, not this call site's, and the next edit
        # that reorders the create would inherit it.
        # `dict(...)`, not the mapping itself: `dict` is invariant in its value type, so a
        # `dict[str, str]` is not a `dict[str, str | None]` — and that signature is the seam's
        # null-DELETE dialect, which this call never uses.
        widened: dict[str, str | None] = dict(properties)
        dataset.update_schema_metadata(widened)
    return CreateTableResponse(location=location, version=dataset.version, properties=properties)


def _existing_location(ns: LanceNamespace, segments: list[str]) -> tuple[str | None, bool]:
    """``(location, is_only_declared)`` for the table, or ``(None, False)`` if it does not exist.

    ``check_declared=True`` makes the namespace surface a declared-but-unwritten table (rather than raising
    TableNotFound), and ``is_only_declared`` distinguishes it from a written one — a declared-only table has
    no readable dataset, so the caller must NOT try to open it (that would 500).
    """
    try:
        resp = ns.describe_table(DescribeTableRequest(id=segments, with_table_uri=True, check_declared=True))
    except TableNotFoundError:
        return None, False
    location = getattr(resp, "table_uri", None) or getattr(resp, "location", None)
    return location, bool(getattr(resp, "is_only_declared", False))


def rename_table(
    ns: LanceNamespace,
    so: StorageOptions,
    segments: list[str],
    new_table_name: str,
    new_namespace_id: list[str] | None,
    *,
    claims: location_claims.ClaimStore,
    delimiter: str,
    root: str = "",
) -> tuple[list[str], str]:
    """Rename a table by moving its POINTER. No data byte is read, written or deleted.

    A rename is a namespace-layer remap — Lance has no format-level rename, and the spec's request
    carries identifiers and no location. lance-ns V2 stores a table at ``<hash>_<object_id>`` with the
    mapping in the ``__manifest`` table; the hash is there for object-store throughput and for
    create/delete/recreate conflict prevention, and the spec says the ``object_id`` suffix "ensures
    uniqueness and aids debugging" (`lance_docs/namespace.md`, *Manifest Table Directory*). It is a
    label, not the resolution path. So the whole rename is: take the location's claim for the
    destination, deregister the source, and register the destination id at the source's location.

    That makes it **O(1) in the dataset**, which is the point. The cost of a rename used to be the
    dataset's size, paid inside a request handler that answered 200 — unbounded work no pod sizing
    fixes, and the same class as the compact door before it became a 202. Three failure modes go with
    it: the half-copy, the non-atomic source delete that could strand bytes, and the read-rewrite that
    would have collapsed version history.

    MEASURED on the ``dir`` backend the chart runs (``LANCE_REST_IMPL=dir``, pylance 10.0.0,
    2026-09-04): after the two calls, ``describe_table`` on the destination resolves to the source's
    own location, with rows and version history intact and the directory keeping its old object_id
    suffix.

    **A V1 ROOT-namespace table is REFUSED.** Compatibility mode stores those at ``<name>.lance``,
    where the location IS the name, and the spec's own rule is that renaming one "transitions to the
    V2 hash-based path naming" — a relocation. Serving it with a byte copy would put the unbounded
    work straight back; rask's ``require_parent`` guard means no table reachable through these doors
    has that shape, so the refusal costs nothing and names the reason.

    Returns ``(new_segments, location)`` — the location UNCHANGED, because that is the property.
    Raises ``TableNotFoundError`` (source missing / declared-only), ``TableAlreadyExistsError``
    (destination taken) or ``ConcurrentModificationError`` (another rename or registration holds the
    location) so the endpoint maps 404 / 409 / 409.

    THE LOCATION CLAIM IS THE ARBITRATION ([[LH-204]]). Before either backend call, the location's
    claim is handed from the source id to the destination id under the store's put-if-not-exists and
    ETag (:mod:`service_kit.lakehouse.location_claims`), so of N concurrent renames of one source exactly
    one holds the location and the rest answer code 14. Nothing in the backend can do this: measured on
    pylance 12.0.0 (lh204 m1), concurrent deregisters of one id all succeed and a register at an occupied
    location commits, so eight barrier-threaded renames left eight live ids on one dataset, 4 of 4 rounds.

    The two backend calls are not one transaction. A crash between them leaves the table reachable by
    no id with its claim naming the destination; a re-register of either id at the location, after the
    claim's lease, recovers it.
    """
    if not new_table_name.strip():
        raise InvalidInputError("new_table_name is required")
    dest_parent = list(new_namespace_id) if new_namespace_id else segments[:-1]
    new_segments = [*dest_parent, new_table_name]
    if new_segments == segments:
        raise InvalidInputError("the rename destination is identical to the source")
    source_uri, source_only_declared = _existing_location(ns, segments)
    if source_uri is None or source_only_declared:
        # Missing OR declared-but-unwritten → nothing to repoint (404, symmetric with every op that
        # requires a written table).
        raise TableNotFoundError(f"table not found: {'.'.join(segments)}")
    if len(segments) == 1:
        raise InvalidInputError(
            f"cannot rename {'.'.join(segments)}: it is a root-namespace table, which the directory catalog stores "
            "under V1 compatibility naming (<name>.lance) where the location IS the name. The spec's rule is that such "
            "a rename transitions the table to V2 hash-based naming, which relocates its data — unbounded work this "
            "door will not do in a request. Move the table into a namespace first."
        )
    # The destination must be free in EVERY form, checked BEFORE anything is touched. A declared-only
    # stub is TAKEN, never adopted: adopting one skips `register_table`, which is what arbitrates two
    # concurrent renames into the SAME destination name — the source-side race is arbitrated by the
    # location claim below, and the two together are what make this door safe under concurrency.
    dest_uri, _dest_only_declared = _existing_location(ns, new_segments)
    if dest_uri is not None:
        raise TableAlreadyExistsError(f"table already exists: {'.'.join(new_segments)}")
    # RELATIVE, because the dir backend refuses an absolute URI here ("Absolute URIs are not allowed
    # for register_table") — the same conversion `undrop` already makes. `_relative_to_root` derives
    # it from the namespace's own root rather than assuming the final path segment: under V2 naming a
    # location is `<hash>_<object_id>` at the root, but a backend that nests would break the guess.
    # NO BRANCH GUARD, and its absence is a decision. A branch is a shallow clone referencing its
    # source root by ABSOLUTE path, so the byte-copy rename this door used to perform orphaned every
    # branch — it copied the root, deleted the source, and answered 200 over unreadable branch data.
    # The pointer move copies and deletes nothing: measured on the `dir` backend, `branches.list()`
    # after a rename returns the identical entry and the data reads back, because the bytes never
    # moved. Re-adding a refusal would decline a safe operation.
    location = _relative_location(source_uri, root=root)
    # THE CLAIM IS HANDED OVER FIRST, and it is the whole of this door's race arbitration: the loser of
    # two renames of one source reads the winner's claim and answers code 14 before touching the
    # backend. The source is still retired before the destination is written, so a rename never shows
    # two ids on the dataset even for the instant between the two calls. The token makes this request's
    # claim its own: two renames into the SAME destination name one holder, and only the request that
    # stamped the claim may read it as its retry.
    source_id, dest_id = delimiter.join(segments), delimiter.join(new_segments)
    try:
        table_claims.take(ns, claims, source_uri, dest_id, new_segments, previous=source_id, token=uuid.uuid4().hex)
    except location_claims.LocationHeldError as held:
        raise ConcurrentModificationError(
            f"cannot rename {source_id}: its location is held by {held.claim.table}, by a concurrent rename or registration; describe it and retry"
        ) from None

    def _hand_back() -> None:
        # The source keeps its location, so it keeps the claim; left with the destination, every later
        # rename of the source would answer code 14 for the claim's lease. Logged, never raised over the
        # failure that made the rename compensate.
        try:
            table_claims.take(ns, claims, source_uri, source_id, segments, previous=dest_id)
        except Exception as exc:
            log.error("rename_claim_not_handed_back", extra={"source": source_id, "location": source_uri, "error": str(exc)[:300]})

    try:
        ns.deregister_table(DeregisterTableRequest(id=segments))
    except Exception:
        _hand_back()
        raise
    try:
        ns.register_table(RegisterTableRequest(id=new_segments, location=location))
    except Exception as failed:
        # A destination that now resolves to this location is another request's finished rename: putting
        # the source back would make a second live id on the dataset, so its state stands and this one
        # answers code 14.
        if isinstance(failed, TableAlreadyExistsError) and _resolves_to(ns, new_segments, source_uri):
            raise ConcurrentModificationError(f"cannot rename {source_id}: a concurrent rename already moved it to {dest_id}") from None
        # COMPENSATE, or a rename that trips on its destination leaves the table reachable by NO id —
        # bytes intact and invisible. The same call at the same location the source already had, and
        # the claim handed back with it.
        try:
            ns.register_table(RegisterTableRequest(id=segments, location=location))
        except Exception as restore:
            # The source is now unreachable and the location is the only way back, so it goes in the
            # log rather than being swallowed with the original error.
            log.error(
                "rename_source_pointer_lost",
                extra={"source": source_id, "location": location, "error": str(restore)},
            )
        else:
            _hand_back()
        raise
    return new_segments, source_uri


def _resolves_to(ns: LanceNamespace, segments: list[str], location: str) -> bool:
    """Whether ``segments`` is registered and describes ``location``; an unreadable answer is ``False``."""
    try:
        described = ns.describe_table(DescribeTableRequest(id=segments))
    except Exception:  # noqa: BLE001 — not provably the winner's table, so the caller compensates as before
        return False
    return decoded_path(str(described.location or "")) == decoded_path(location)


def _relative_location(uri: str, *, root: str) -> str:
    """``uri`` expressed relative to the namespace ROOT, which is what ``register_table`` accepts.

    The dir backend refuses an absolute URI outright and the value `describe_table` reports IS
    absolute, so every re-registration in this estate makes this conversion (`undrop` included).

    THE ROOT IS PASSED IN because the namespace object does not carry one. Measured on the installed
    client: `DirectoryNamespace` exposes no `root` attribute and nothing root-shaped at all, so a
    `getattr(ns, "root", "")` was always empty — the subtraction never ran and every rename took the
    leaf fallback below. The caller connects the namespace and holds `settings.root`, so it is the
    one place that can answer.

    Subtracting the root matters only where a backend NESTS: under V2's flat `<hash>_<object_id>`
    layout the leaf and the relative path are the same string, and they diverge the moment they are
    not — where registering the leaf would point at nothing. An absent root falls back to the leaf,
    which is correct for that flat case and is now the stated behaviour of having no root rather than
    the only branch that could execute.
    """
    for candidate in (root, root.removeprefix("file://")):
        if candidate and (stripped := uri.removeprefix("file://")).startswith(candidate.rstrip("/") + "/"):
            return stripped[len(candidate.rstrip("/")) + 1 :]
    return uri.removeprefix("file://").rstrip("/").rsplit("/", 1)[-1]


def _dataset_fs(uri: str, so: StorageOptions) -> tuple[pafs.FileSystem, str]:
    """Resolve ``uri`` to a ``(filesystem, path)`` pair for a dataset relocation.

    A local ``file://`` / bare-path location (the ``dir`` backend default) needs no credentials; an
    ``s3://`` location builds an ``S3FileSystem`` from the SAME storage options the datasets are opened
    with (path-style, http-ok endpoint) — mirroring the media head's filesystem so RustFS/MinIO work.
    """
    if uri.startswith("s3://") and so.get("endpoint"):
        return s3_filesystem(so), uri[len("s3://") :]
    resolved, path = pafs.FileSystem.from_uri(uri)
    return resolved, path


#: What an append with nothing to append onto is told, by the commit classifier and the file-version guard alike.
_NO_BASE_DETAIL = "append target has no committed base version — create or overwrite the table first"

#: The default non-retryable remedy — the client-direct append's. Its fragments describe data the table
#: no longer accepts, so the data must be written again, not the metadata re-sent.
_APPEND_REMEDY = "Discard them, re-read the current version, and re-WRITE the data"


def _classify_commit_error(exc: OSError, *, remedy: str = _APPEND_REMEDY) -> Exception:
    """Map a raised ``LanceDataset.commit`` ``OSError`` to the right catalog error (audit 2026-07-14).

    THE VERDICT IS SHARED, THE ERROR TYPE IS NOT. `service_kit.lancekit.commit_verdict` owns the
    vocabulary — which phrases mean which outcome, and crucially the ORDER they are tested in — because
    two planes were classifying the same condition and had drifted: this one carried the full taxonomy
    while `lancekit/writer.py` carried two markers and could not see the non-retryable case at all,
    answering it `409 re-read and re-send`, which is the advice below explains you must never give.
    Each plane still raises its own errors (`lance_namespace` here, `DomainError` there), so what
    crosses is the verdict.

    ``remedy`` is the sentence the non-retryable branch gives the caller — the work it must redo. It is
    per-door because the doors hold different things: an append holds fragments it must re-write, a
    compaction holds a result whose plan is void.
    """
    match classify_commit_failure(exc):
        case CommitVerdict.NO_BASE:
            return InvalidInputError(f"{_NO_BASE_DETAIL}: {exc}")
        case CommitVerdict.CLIENT_ERROR:
            return InvalidInputError(f"fragments are incompatible with the table: {exc}")
        case CommitVerdict.INCOMPATIBLE:
            # NON-RETRYABLE (spec). Do NOT tell the caller to re-commit: the table changed underneath them
            # (e.g. a concurrent Overwrite), so what they hold describes data that no longer belongs. Advising
            # a re-commit here is how you corrupt a table. The REMEDY differs by door — an append re-writes its
            # fragments, a compaction re-plans and re-executes — so the caller names the work it must redo.
            return InvalidInputError(
                "commit is incompatible with the table's current transaction — this is NOT retryable: the "
                f"table changed underneath it (e.g. a concurrent overwrite). {remedy}; do not re-commit: {exc}"
            )
        case CommitVerdict.RETRYABLE_CONFLICT:
            return ConcurrentModificationError(f"commit conflict — re-read the table version and re-commit the fragments: {exc}")
        case CommitVerdict.STORE_UNAVAILABLE:
            return ServiceUnavailableError(f"object store unavailable during commit: {exc}")


def _fragment_signature(fragment: lance.FragmentMetadata) -> str:
    """The fragment as a commit stores it.

    Measured on pylance 12.0.0: an Append's transaction keeps each fragment exactly as sent (id, data files
    and their bases, sizes, field ids, row count, deletion file), so equal signatures read the same rows.
    Both sides pass through pylance's ``to_json``, so a fragment serialized on another pylance compares by
    content rather than by spelling.
    """
    return json.dumps(fragment.to_json(), sort_keys=True)


def _read_transactions(location: str, so: StorageOptions, versions: Sequence[int]) -> list[tuple[int, lance.Transaction | None, Exception | None]]:
    """Each version's transaction in version order, with any failure carried for the caller to judge.

    One handle per worker thread: measured on pylance 12.0.0, eight threads reading transactions through
    one ``LanceDataset`` raised ``RuntimeError: Already borrowed`` in 20 of 20 rounds over 40 versions, and
    in 0 of 20 with a handle each.
    """
    local = threading.local()

    def _read(version: int) -> tuple[int, lance.Transaction | None, Exception | None]:
        try:
            handle: lance.LanceDataset | None = getattr(local, "dataset", None)
            if handle is None:
                handle = local.dataset = lance.dataset(location, storage_options=dict(so) if so else None, session=shared_lance_session())
            return version, handle.read_transaction(version), None
        except Exception as exc:
            return version, None, exc

    with ThreadPoolExecutor(max_workers=min(8, len(versions))) as pool:
        return list(pool.map(_read, versions))


def _held_signature(fragment: dict[str, Any]) -> str:
    """A fragment as a version's manifest holds it: its data files and its row count.

    Measured on pylance 12.0.0: a commit keeps each fragment's files and physical_rows exactly and assigns
    id, row_id_meta and the two version metas, which are left out.
    """
    return json.dumps({"files": fragment.get("files"), "physical_rows": fragment.get("physical_rows")}, sort_keys=True)


def _version_holds(location: str, so: StorageOptions, version: int, fragments: Sequence[lance.FragmentMetadata]) -> bool:
    """Whether ``version``'s manifest holds every one of these fragments with no deletion file: what a reader of that version reads."""
    try:
        held = [
            f.metadata.to_json()
            for f in lance.dataset(location, version=version, storage_options=dict(so) if so else None, session=shared_lance_session()).get_fragments()
        ]
    except Exception as exc:
        if reads_as_absent(exc):
            return False
        raise ServiceUnavailableError(f"cannot read version {version} of {location!r} to confirm it holds the run's fragments: {exc}") from exc
    present = {_held_signature(fragment) for fragment in held if fragment.get("deletion_file") is None}
    return {_held_signature(fragment.to_json()) for fragment in fragments} <= present


def _rows_at(location: str, so: StorageOptions, version: int) -> int:
    return lance.dataset(location, version=version, storage_options=dict(so) if so else None, session=shared_lance_session()).count_rows()


def _find_run_commit(location: str, so: StorageOptions, fragments: Sequence[lance.FragmentMetadata], *, read_version: int, run_id: str) -> int | None:
    """The version after ``read_version`` whose operation added every one of these fragments.

    A committer whose answer was lost retries with the same fragments, and Append never conflicts with
    Append (transaction.md), so only recognizing the earlier commit keeps its rows from landing twice. The
    fragments are the evidence, never a run id, which any writer of the table can put on a commit
    ([[LH-280]]): whoever committed them, the rows are in the table as sent. A version counts only when its
    manifest holds the fragments too: the transaction record is evidence the version's writer chose, while
    the manifest is what a reader of that version reads.

    Fails closed: an error that proves absence reads as "not committed" (no table, a stranger's version
    with no transaction file); any other error raises, because appending again on an unread store risks
    the duplicate this exists to prevent.
    """
    try:
        dataset = lance.dataset(location, storage_options=dict(so) if so else None, session=shared_lance_session())
    except Exception as exc:
        if not reads_as_absent(exc):
            raise ServiceUnavailableError(
                f"cannot determine whether run {run_id!r} already committed to {location!r} — the object store is unreadable, "
                f"and proceeding would risk appending the same rows twice: {exc}"
            ) from exc
        return None
    # `version_refs()`, not `versions()`: pylance answers it without reading a manifest per version.
    candidates = [version for version_info in dataset.version_refs() if (version := int(version_info["version"])) > read_version]
    if not candidates:
        return None
    wanted = {_fragment_signature(fragment) for fragment in fragments}
    for version, transaction, exc in _read_transactions(location, so, candidates):
        if exc is not None:
            if not reads_as_absent(exc):
                raise ServiceUnavailableError(
                    f"cannot read version {version} while checking whether run {run_id!r} already committed — "
                    f"this may be the run's own commit, and skipping it would append the rows twice: {exc}"
                ) from exc
            continue
        if transaction is None:
            continue
        added = list(getattr(transaction.operation, "fragments", None) or ())
        if wanted <= {_fragment_signature(fragment) for fragment in added} and _version_holds(location, so, version, fragments):
            return version
    return None


def _recorded_run_commit(location: str, so: StorageOptions, run: commit_runs.CommitRun) -> tuple[int, int] | None:
    """The version the catalog recorded ``run`` committing and the row count there, while that version exists."""
    try:
        recorded = commit_runs.read_committed(run, location)
    except OSError as exc:
        raise ServiceUnavailableError(f"cannot read what run {run.run_id!r} committed to {location!r}: the control root is unreadable: {exc}") from exc
    if recorded is None:
        return None
    try:
        return recorded.version, _rows_at(location, so, recorded.version)
    except Exception as exc:
        if reads_as_absent(exc):
            return None
        raise ServiceUnavailableError(f"cannot read version {recorded.version}, which run {run.run_id!r} committed to {location!r}: {exc}") from exc


def _record_run_commit(run: commit_runs.CommitRun, location: str, *, version: int) -> None:
    try:
        commit_runs.record_committed(run, location, version=version)
    except OSError as exc:
        raise ServiceUnavailableError(
            f"run {run.run_id!r} committed version {version} to {location!r}, and the catalog could not record it: {exc}. "
            "A retry of the same fragments is answered with that version"
        ) from exc


def _prior_run_commit(
    location: str, so: StorageOptions, frags: Sequence[lance.FragmentMetadata], *, read_version: int, run: commit_runs.CommitRun
) -> tuple[int, int] | None:
    """What ``run`` already committed to the table, as ``(version, row_count)``, or ``None`` when this commit must land.

    Fragments that already landed after ``read_version`` answer with the version holding them. Otherwise
    the catalog's record of this caller's run answers: always for an EMPTY commit (the "what did I commit?"
    probe), and for a commit carrying fragments only when the recorded version is newer than
    ``read_version``, because a retry can resend a different list for a run that already committed (ingest
    falls back to its carried list once the staged one is purged).
    """
    if frags:
        landed = _find_run_commit(location, so, frags, read_version=read_version, run_id=run.run_id)
        if landed is not None:
            _record_run_commit(run, location, version=landed)
            return landed, _rows_at(location, so, landed)
    recorded = _recorded_run_commit(location, so, run)
    if recorded is None or (frags and recorded[0] <= read_version):
        return None
    return recorded


def commit_appended_fragments(
    location: str,
    so: StorageOptions,
    fragments: list[dict[str, Any]],
    read_version: int,
    *,
    run: commit_runs.CommitRun | None = None,
    external_blob_bases: Sequence[base_registry.RecordedBase] = (),
) -> tuple[int, int]:
    """Commit client-written fragments as an APPEND — the catalog as the governed commit coordinator (#2).

    The client wrote the data fragments DIRECTLY to object storage with vended, table-scoped creds
    (``lance.fragment.write_fragments`` — the guide's distributed-write protocol); this folds the tiny
    serialized ``FragmentMetadata`` into a metadata-ONLY Lance commit under ROOT creds, so no data byte ever
    transits the catalog and the version + lineage emit stay atomic (the External Manifest Store pattern —
    namespace.md). ``write_fragments`` inherits the table's ``data_storage_version`` only when the client
    names none; pylance 12 commits fragments at another version and stamps the sticky reader flag 256, so
    :func:`_refuse_foreign_file_versions` judges the fragments' file versions before Lance sees them. Lance
    assigns/rebases stable row ids at commit (row_id_lineage.md). CREATE and OVERWRITE stay server-side to
    centralize the 2.2 invariant and to owner-govern the destructive reset. Append auto-rebases against
    concurrent appends and conflicts only with Overwrite/Restore (transaction.md); a stale/incompatible
    commit raises, mapped by :func:`_classify_commit_error`.

    With ``run``, a retry is idempotent: fragments that already landed are answered with the version that
    holds them (:func:`_find_run_commit`), and an EMPTY commit is answered with what the catalog recorded
    this caller's run committing, or refused when it recorded nothing.

    Returns:
        ``(version, row_count)``: the committed version and the table's row count at it.

    The fragments are a claim and the data files are the authority (:mod:`catalog.services.client_fragments`):
    nothing an append cannot carry, each file at its declared size, and each footer agreeing with the
    fragment's rows, columns, field ids and file version. A table with blob columns is committed detached
    first, so every blob sidecar its descriptors point into is read before the version is published.
    ``external_blob_bases`` is the catalog's record of the external blob bases the table's create
    authorized ([[LH-209]]); an external descriptor resolving through any other base is refused.

    Raises:
        InvalidInputError: Malformed fragments, no fragments (and no recorded run commit), a based data
            file, a foreign file version, a data file missing under the table or at another size, a
            footer or blob sidecar that contradicts the fragment, or metadata an append cannot carry.
        ServiceUnavailableError: The object store or the control root could not be read or written.
    """
    if read_version < 0:
        raise InvalidInputError(f"read_version must be non-negative, got {read_version}")
    # Client-controlled input: a malformed fragment dict raises KeyError/TypeError/ValueError from
    # ``from_json`` (outside the OSError taxonomy) — translate to a 400, never a 500 (audit 2026-07-14).
    try:
        frags = [lance.FragmentMetadata.from_json(json.dumps(f)) for f in fragments]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise InvalidInputError(f"malformed fragment metadata: {exc}") from exc
    # The run checks come before the empty guard and every check of the files: a retry may arrive after
    # maintenance compacted its files away, and an empty commit carrying a run is a question only the
    # record can answer.
    if run is not None:
        prior = _prior_run_commit(location, so, frags, read_version=read_version, run=run)
        if prior is not None:
            return prior
    if not frags:
        raise InvalidInputError("no fragments to commit")
    _refuse_based_data_files(fragments)
    client_fragments.refuse_forged_metadata(frags)
    judged_version, judged_against = _refuse_foreign_file_versions(location, so, frags, read_version)
    client_fragments.refuse_files_the_table_holds(frags, judged_against)
    # HIGH (audit 2026-07-14): Lance's commit validates NEITHER data-file existence NOR the declared row
    # count, so a client whose direct write landed under a DIFFERENT prefix than the catalog-resolved
    # location (no malice required) could otherwise commit a 200-OK-but-UNREADABLE current version that
    # breaks reads for EVERY reader until an operator restores. Pre-verify the files exist under the table
    # location; a failed check leaves the table untouched (400) instead of poisoning its current version.
    _verify_fragment_data_files(location, so, fragments)
    # The files exist at their declared sizes; their footers now say whether they hold what is declared ([[LH-211]]).
    client_fragments.verify_against_footers(location, so, frags, judged_against)
    op = lance.LanceOperation.Append(frags)
    if columns := client_fragments.blob_columns(judged_against):
        _verify_blob_sidecars(location, so, op, frags, read_version=judged_version, columns=columns, external_bases=external_blob_bases)
    try:
        dataset = lance.LanceDataset.commit(location, op, read_version=judged_version, storage_options=so)
    except OSError as exc:
        raise _classify_commit_error(exc) from exc
    version = int(dataset.version)
    if run is not None:
        _record_run_commit(run, location, version=version)
    return version, dataset.count_rows()


def _refuse_based_data_files(fragments: list[dict[str, Any]]) -> None:
    """Refuse any client fragment whose data file names a base (a non-null ``base_id``) ([[LH-279]]).

    ``base_id`` indexes the TABLE's base list, which a write vend can extend with ``add_bases`` — and
    ``add_bases`` with an existing id REPOINTS that base (lh279 m4) — so a fragment resolving through a
    base is a pointer the writer chose into bytes the writer need not own: measured, an appended fragment
    through a planted base made the victim's rows the attacker's (lh279 m2). No rule judged at
    ``read_version`` can bound it, because the base list the commit lands on is the writer's too.

    Refusing the primitive costs no legitimate producer: every base a vend reaches is granted READ only,
    and the one ``/commit`` client (ingest's lander) calls ``write_fragments`` without ``target_bases``,
    so its files carry ``base_id`` null. A table spread across bases is written server-side at create.

    Raises:
        InvalidInputError: A data file carries a non-null ``base_id``; nothing was read or committed.
    """
    based = [
        (index, data_file.get("path"))
        for index, frag in enumerate(fragments)
        for data_file in (frag.get("files") if isinstance(frag, dict) else None) or []
        if isinstance(data_file, dict) and data_file.get("base_id") is not None
    ]
    if based:
        log.warning("catalog_commit_refused_based_data_file", extra={"files": based[:5], "count": len(based)})
        raise InvalidInputError(
            f"commit refused: {len(based)} data file(s) resolve through a base (non-null base_id), e.g. fragment/file {based[:5]}. "
            "A client commit may only append files under the table's own data/ directory; write them there with write_fragments and no target_bases"
        )


def _refuse_foreign_file_versions(
    location: str, so: StorageOptions, frags: Sequence[lance.FragmentMetadata], read_version: int
) -> tuple[int, lance.LanceDataset]:
    """Refuse fragments whose data files would mix the table's file versions.

    Returns the version the commit is made at, and the table version the files' declared format matched,
    which their footers are then held to.

    pylance 12.0.0 commits them and stamps reader flag 256, which no operation removes, so the refusal
    has to come before the commit. The table is judged at ``read_version``, the version the caller built
    against: an Overwrite since then is Lance's own non-retryable conflict, and judging at the latest
    version would answer it with the wrong error. ``read_version`` 0 names no manifest, so the latest is
    judged, and the caller commits at the version returned: a commit at 0 runs no conflict check at all,
    so an Overwrite landing between this read and the commit would slip unjudged files onto another
    version (measured on 12.0.0). A base that cannot be read fails closed.
    """
    try:
        base = lance.dataset(location, version=read_version or None, storage_options=dict(so) if so else None, session=shared_lance_session())
    except Exception as exc:
        if reads_as_absent(exc):
            raise InvalidInputError(f"{_NO_BASE_DETAIL}: {exc}") from exc
        raise ServiceUnavailableError(f"cannot read the table to judge the fragments' file versions, so nothing was committed: {exc}") from exc
    # THE VERSION THE CALLER BUILT AGAINST, judged like any read ([[LH-279]]): an append onto a table whose
    # manifest declares a base nothing sanctioned carries that base into the version it mints.
    require_sanctioned_bases(location, manifest_base_path_refs(base), judge=None)
    files = [data_file for frag in frags for data_file in frag.files]
    reason = describe_foreign_data_file_versions(base.data_storage_version, files)
    if reason is None:
        return int(base.version), base
    if read_version and (latest := _latest_if_it_matches(location, so, files)) is not None:
        # Written after an Overwrite moved the table to their version: Lance's own non-retryable
        # conflict is the true answer, and it refuses the commit without setting the flag.
        return int(base.version), latest
    log.warning(
        "catalog_commit_refused_foreign_file_version",
        extra={
            "location": location,
            "read_version": read_version,
            "table_version": base.data_storage_version,
            "fragment_versions": sorted({f"{d.file_major_version}.{d.file_minor_version}" for d in files}),
        },
    )
    raise InvalidInputError(
        f"{reason}. Re-read the table's current version and write the fragments against it without data_storage_version "
        f"(write_fragments into an existing table inherits it). A committed mix cannot be undone"
    )


def _latest_if_it_matches(location: str, so: StorageOptions, files: Sequence[VersionedDataFile]) -> lance.LanceDataset | None:
    """The table's LATEST version when ``files`` are all at its file version, else ``None``. Read only on the refusal path."""
    latest = lance.dataset(location, storage_options=dict(so) if so else None, session=shared_lance_session())
    return latest if describe_foreign_data_file_versions(latest.data_storage_version, files) is None else None


def _verify_fragment_data_files(location: str, so: StorageOptions, fragments: list[dict[str, Any]]) -> None:
    """Reject a commit that references data files ABSENT under ``<location>/data/`` (audit HIGH fix).

    Each fragment's ``files[].path`` is a bare filename under the dataset's ``data/`` dir: a file naming a
    base was already refused by :func:`_refuse_based_data_files`. A missing file means the client's direct
    write targeted the wrong prefix — committing it would publish an unreadable version, so raise 400 and
    leave the table as-is. A declared ``file_size_bytes`` must be the object's size: Lance reads the footer
    at the declared size, so a larger one failed every read of the table (measured, [[LH-211]]). An absent
    size is legitimate, and Lance asks the store.
    """
    fs, base = _dataset_fs(location, so)
    prefix = base.rstrip("/") + "/data/"
    # Collect every root-relative data file first, then resolve them in ONE batched lookup: pyarrow's
    # list overload runs the object-store lookups concurrently, so an N-fragment commit costs one round
    # trip instead of N serial HEADs on the commit hot path.
    declared = [
        (str(rel), data_file.get("file_size_bytes"))
        for frag in fragments
        for data_file in (frag.get("files") if isinstance(frag, dict) else None) or []
        if isinstance(data_file, dict) and (rel := data_file.get("path"))
    ]
    infos = fs.get_file_info([prefix + rel for rel, _ in declared]) if declared else []
    missing = [rel for (rel, _), info in zip(declared, infos, strict=True) if info.type == pafs.FileType.NotFound]
    if missing:
        raise InvalidInputError(
            f"commit references {len(missing)} data file(s) not present under the table location (did the direct write target the wrong prefix?): {missing[:5]}"
        )
    resized = [(rel, size, info.size) for (rel, size), info in zip(declared, infos, strict=True) if size is not None and size != info.size]
    if resized:
        raise InvalidInputError(
            f"commit refused: {len(resized)} data file(s) declare a file_size_bytes the object does not have, as (path, declared, actual): {resized[:5]}"
        )


def _verify_blob_sidecars(
    location: str,
    so: StorageOptions,
    op: lance.LanceOperation.Append,
    frags: Sequence[lance.FragmentMetadata],
    *,
    read_version: int,
    columns: Sequence[str],
    external_bases: Sequence[base_registry.RecordedBase],
) -> None:
    """Commit ``op`` DETACHED, read every blob sidecar its descriptors point into, then discard the detached version.

    A detached commit never becomes the latest version (pylance's ``LanceDataset.commit``), so the
    table's readers never see it. Lance's own cleanup does not remove a detached manifest: after
    ``cleanup_old_versions(older_than=0)`` the ``_versions/d<version>.manifest`` was still there while
    the commit's ``.txn`` was gone (measured on 12.0.0), so the door deletes the manifest itself.
    """
    try:
        detached = lance.LanceDataset.commit(location, op, read_version=read_version, storage_options=so, detached=True)
    except OSError as exc:
        raise _classify_commit_error(exc) from exc
    try:
        client_fragments.verify_blob_sidecars(detached, frags, columns, external_bases=external_bases, object_sizes=partial(_object_sizes, so=so))
    finally:
        _discard_detached_manifest(location, so, int(detached.version))


def _object_sizes(uris: Sequence[str], *, so: StorageOptions) -> list[int | None]:
    """Each object's size, ``None`` for one that is absent: one batched lookup per filesystem, as :func:`_verify_fragment_data_files` does."""
    if so.get("endpoint") and all(uri.startswith("s3://") for uri in uris):
        infos = s3_filesystem(so).get_file_info([uri.removeprefix("s3://") for uri in uris])
    else:
        infos = [fs.get_file_info(path) for fs, path in (_dataset_fs(uri, so) for uri in uris)]
    return [None if info.type != pafs.FileType.File else int(info.size) for info in infos]


def _discard_detached_manifest(location: str, so: StorageOptions, version: int) -> None:
    """Delete ``_versions/d<version>.manifest``, writing nothing else.

    S3 goes through the storage seam's plain DeleteObject: pyarrow's ``S3FileSystem.delete_file`` PUTs
    the parent's directory marker after the delete, which left a ``_versions/`` object beside the
    table's manifests (measured on moto). The ``d`` prefix keeps the name apart from every manifest on
    the version line. A delete that fails leaves an unreachable manifest, never a wrong table, so it is
    logged rather than raised.
    """
    relative = f"_versions/d{version}.manifest"
    try:
        if location.startswith("s3://") and so.get("endpoint"):
            bucket, prefix = split_s3_uri(location.rstrip("/"))
            access_key, secret_key, session_token = credential_of(so)
            client = s3_client(so["endpoint"], access_key=access_key, secret_key=secret_key, session_token=session_token, region=so.get("region"))
            client.delete_object(Bucket=bucket, Key=f"{prefix}/{relative}")
        else:
            fs, base = _dataset_fs(location, so)
            fs.delete_file(f"{base.rstrip('/')}/{relative}")
    except Exception:
        log.warning("catalog_commit_detached_manifest_left", extra={"location": location, "manifest": relative}, exc_info=True)


#: pylance's stub declares neither ``CompactionTask.json()`` nor ``RewriteResult.from_json()``, though
#: both exist on the Rust classes and round-trip (verified 2026-09-03 against pylance's shipped
#: ``lance/lance/optimize.pyi``, which stops at ``execute``/``plan``/``commit``). One named alias is where
#: that gap is absorbed — narrow enough that everything else in the two functions below stays checked.
_RewriteResult: Any = lance_optimize.RewriteResult

#: The POLICY half of what the plan door accepts: what shape the table should end up in, and how its
#: bytes move. Optional — an absent knob leaves Lance's own choice, which for these is a shape or a
#: re-encode rather than a memory ceiling.
#:
#: ``compaction_mode`` is here because the in-pod rewrite passes it (``MAINTENANCE_REPACK_MODE``) and
#: Lance bakes it into the task at plan time, so a plan without it would repack differently from the
#: same sweep in-pod. The rest of ``CompactionOptions`` (buffer sizes, index-remap strategy) stays out:
#: nothing in this estate sets them. Names are Lance's, verbatim: ``Compaction.plan`` raises
#: ``ValueError: Invalid compaction option`` for any other (``lance/optimize.py``, pylance 12.0.0).
_COMPACTION_POLICY_KNOBS = (
    "target_rows_per_fragment",
    "max_rows_per_group",
    "max_bytes_per_file",
    "materialize_deletions",
    "materialize_deletions_threshold",
    "compaction_mode",
)

#: The executor's MEMORY BOUNDS, which every plan must carry. Measured on pylance 12.0.0 (2026-09-25):
#: a planned task's JSON bakes all three into its ``options`` and ``CompactionTask.execute`` takes only
#: the dataset, so the plan is the executor's one chance to state them. Left null they become Lance's
#: defaults: "The default `batch_size` is 8192 rows" and a compute pool "determined by the number of
#: cores on the machine" (``lance_docs/guide.md`` § Scanning Data, § Threading Model) — against ~1.8 MB
#: rows, ~15 GB per thread on the HOST's core count — and ``max_source_bytes`` "(default: None, no
#: limit)" on what one run takes on (``lance/optimize.py``). Forwarded, never interpreted: the numbers
#: are the executor's own.
COMPACTION_EXECUTOR_BOUNDS = ("batch_size", "num_threads", "max_source_bytes")

#: The compaction result's non-retryable remedy. A lost race voids the PLAN, not just the commit — the
#: fragments the result names were chosen against a version that no longer exists.
_COMPACTION_REMEDY = "Discard this result and re-plan against the current version"


class PlannedCompaction(BaseModel):
    """What the catalog hands a queue: the version the work was planned against, and the tasks.

    Each task is Lance's own serialized ``CompactionTask`` — an opaque JSON string the catalog does not
    interpret. Opaque is deliberate: the task names fragments and encoding decisions that are the
    format's business, and a catalog that parsed them would have to be re-taught on every format change.

    Named for the plan rather than after Lance's own ``CompactionPlan``, which this is built FROM and
    which is a different type living one import away in the same module.
    """

    read_version: int
    tasks: list[str]


class CompactionOutcome(BaseModel):
    """The metadata-only commit's result: the version it minted and what the rewrite moved."""

    version: int
    fragments_added: int
    fragments_removed: int
    files_added: int
    files_removed: int


def plan_compaction(
    location: str,
    so: StorageOptions,
    *,
    batch_size: int | None,
    num_threads: int | None,
    max_source_bytes: int | None,
    branch: str | None = None,
    **policy: Any,
) -> PlannedCompaction:
    """Plan a compaction WITHOUT executing it — the catalog's first half of the maintenance protocol.

    This is a metadata read: it opens the manifest, decides which fragments should merge, and returns
    serialized tasks for a queue. It moves no data byte and mints no version, which is what makes it
    safe to run inside a request handler at all. The second half is :func:`commit_compaction`; between
    them sits a WORKER holding vended, table-scoped creds that runs ``CompactionTask.execute`` and does
    every byte of the rewrite — the split that keeps the catalog's memory ceiling a function of its
    request rate rather than of the largest table anyone owns.

    ``batch_size``, ``num_threads`` and ``max_source_bytes`` are the executor's memory bounds
    (``COMPACTION_EXECUTOR_BOUNDS``) and all are REQUIRED: a ``None`` in any refuses the plan, naming
    every one, before the manifest is opened. They have no default because the default would be
    Lance's: a batch counted in rows, on every core of the host, over a plan with no byte limit.

    ``policy`` accepts only ``_COMPACTION_POLICY_KNOBS``; anything else is refused rather than dropped,
    so a caller tuning a knob this door does not honour learns it instead of watching the plan ignore it.
    An empty ``tasks`` list is the ANSWER for a table already at target, not an error — a scheduled sweep
    over a healthy estate must be able to conclude "nothing to do" without paging anyone.
    """
    unknown = sorted(set(policy) - set(_COMPACTION_POLICY_KNOBS))
    if unknown:
        raise InvalidInputError(
            f"unsupported compaction option(s) {unknown}; this door accepts {sorted(_COMPACTION_POLICY_KNOBS + COMPACTION_EXECUTOR_BOUNDS)}"
        )
    bounds = dict(zip(COMPACTION_EXECUTOR_BOUNDS, (batch_size, num_threads, max_source_bytes), strict=True))
    if missing := [name for name, bound in bounds.items() if bound is None]:
        # EVERY NAME, whichever is missing. Memory is their product, so they are one bound: a caller
        # told only about the one it forgot would be refused again for the next.
        raise InvalidInputError(
            f"a compaction plan must state the executor's memory bounds, batch_size, num_threads AND max_source_bytes (missing: {missing}). "
            "Lance bakes all three into every task at plan time and CompactionTask.execute accepts no options, so a plan without them runs "
            "every rewrite on Lance's defaults: the scanner's default batch on every core of the host, with no limit on the bytes one run takes on. "
            "Send the bounds your executor runs under."
        )
    options = {k: v for k, v in policy.items() if v is not None} | bounds
    try:
        dataset = lance.dataset(location, storage_options=dict(so) if so else None, session=shared_lance_session())
        if branch is not None:
            # THE REF THE REQUEST NAMES. `lance.dataset(uri)` opens MAIN whatever the caller asked for, so
            # planning without this returns main's fragments and main's `read_version` labelled as the
            # branch's work — and `commit_compaction` would then have a worker rewrite the wrong ref.
            # `checkout_version((branch, None))` is pylance's own branch reference, the form
            # `core.namespace.open_dataset` uses; this door takes a LOCATION rather than a table id, so it
            # cannot call that helper and applies the ref itself.
            #
            # A BRANCH IS SAFE TO COMPACT WHILE IT IS NOT YET SAFE TO RECLAIM, which is the licence for
            # opening this door while `maintenance/preview|run|compact` stay refused: a compaction rewrites
            # fragments and mints a version, leaving the old files for whatever reclaims them later
            # ([[LH-094]]). It changes which dataset is REWRITTEN, never what is DELETED.
            dataset = dataset.checkout_version((branch, None))
    except ValueError as exc:
        # A REGISTERED TABLE WHOSE BYTES ARE NOT THERE. Measured live 2026-09-04: a table declared into
        # a namespace bound to one warehouse, with its data written to another bucket, reached this
        # line and pylance's "Dataset at path ... was not found" escaped as a bare 500 "Internal
        # Server Error" — which tells an operator nothing and looks like a catalog fault rather than
        # a table that was never written.
        #
        # NARROWED, because `ValueError` is not that condition. Measured on pylance 11.0.0: an absent
        # dataset raises `Dataset at path … was not found`, a malformed storage option raises
        # `LanceError(IO): Generic Config error: failed to parse "x" as Duration`, and a bucket that
        # does not exist raises `Generic S3 error: … 404 Not Found: <Code>NoSuchBucket</Code>` — all
        # three `ValueError`. Reporting any of the last two as "never written" sends an operator to
        # look for data that was there all along, so only a PROVEN absence answers 404.
        #
        # `reads_as_absent` rather than this door's own marker: the question is the estate's, not
        # compaction's, and four seams had each answered it differently. A local `"not found" in ...`
        # was one of them — it matches the HTTP status line every object-store error carries, so a
        # missing warehouse bucket read as a table nobody wrote (§ Q8-15 measured 79 such datasets).
        # AND THE OTHER TWO GET THE ANSWER THE SIBLING ALREADY GIVES. Re-raising them bare returned a
        # 500 — the very complaint above, one class over: a store the catalog could not read reported
        # as a catalog fault. `_already_committed` maps the same condition to `ServiceUnavailableError`
        # (503) on the line that says "the object store is unreadable", which is the honest answer:
        # nothing about the REQUEST is wrong, and the operator's next move is the warehouse, not the
        # policy. The cause is carried in the message rather than swallowed.
        if not reads_as_absent(exc):
            raise ServiceUnavailableError(
                f"cannot read the table at {location!r} to plan a compaction — the object store did not answer for it. "
                f"This is a STORAGE fault, not a bad request: check the warehouse binding, the bucket and the credential: {exc}"
            ) from exc
        # `TableNotFoundError`, not `InvalidInputError`: code 13 tells a client its REQUEST is
        # malformed and nothing about the policy is. What is absent is the DATA, which code 4,
        # TableNotFound, names (spec.yaml:2416) — the same answer `rename_table` gives a source that
        # resolves to nothing, so one client branch serves both.
        raise TableNotFoundError(
            f"no dataset exists at this table's location ({location}) — it is declared or registered but was never written: {exc}"
        ) from exc
    # A REWRITE THROUGH AN UNSANCTIONED BASE copies the bytes behind it into this table's own root
    # ([[LH-279]]), which is the read the base judge refuses, made permanent.
    require_sanctioned_bases(location, manifest_base_path_refs(dataset), judge=None)
    plan = lance_optimize.Compaction.plan(dataset, cast(Any, options))
    return PlannedCompaction(read_version=int(plan.read_version), tasks=[cast(str, cast(Any, task).json()) for task in plan.tasks])


def commit_compaction(location: str, so: StorageOptions, results: Sequence[str], *, branch: str | None = None) -> CompactionOutcome:
    """Commit the workers' rewrite results — the catalog's second half, under ROOT creds.

    Metadata-only by construction: every data file this publishes was written by the worker that
    executed the task, and already exists when this is called. The catalog reads none of them.

    ``results`` are Lance's serialized ``RewriteResult`` strings, arriving off a queue and therefore
    CLIENT-CONTROLLED: ``from_json`` raises ``ValueError`` on a malformed one, which becomes a 400 here
    rather than a 500. An EMPTY list is refused for the same reason the sibling append door refuses
    empty fragments — Lance answers it with a zero-metric success and no new version, which relayed
    verbatim is indistinguishable from a healthy table and would mask an executor that lost every result
    it was handed.
    """
    if not results:
        raise InvalidInputError("no compaction results to commit")
    try:
        rewrites = [_RewriteResult.from_json(result) for result in results]
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvalidInputError(f"malformed compaction result: {exc}") from exc
    dataset = lance.dataset(location, storage_options=dict(so) if so else None, session=shared_lance_session())
    if branch is not None:
        # THE SAME REF THE PLAN READ. `plan_compaction` returns a `read_version` from the branch, and Lance
        # commits a rewrite against the ref it was planned on — committing here on main would apply the
        # worker's results to a dataset whose fragments the plan never saw.
        dataset = dataset.checkout_version((branch, None))
    # THE SAME GATE THE BUTTON ASKS, and asked HERE rather than at plan time because this is the half
    # that mints a version and drops the old fragments — a plan nobody commits costs nothing.
    #
    # The estate exposes two routes onto one operation: `/maintenance/compact` runs through
    # `maintenance.compact_now`, which calls `require_compactable` (the feature-flag evidence gate plus
    # the #114 shallow-clone base-refs guard), while this pair reached `lance_optimize` directly and
    # asked neither. Both are writer-tier and published at the ingress under `/api/catalog`, so a caller
    # who took the distributed route got a rewrite the button refuses WITH A REASON — including on a
    # real shallow clone, where compacting materialises the shared base into the clone's own root.
    #
    # Imported here rather than at module scope: `catalog.services.maintenance` imports this module, so
    # a top-level import is a cycle.
    from catalog.services.maintenance import require_compactable

    require_sanctioned_bases(location, manifest_base_path_refs(dataset), judge=None)
    require_compactable(dataset, so)
    # The rewrites are client-supplied, so their files are judged like an append's: a forged or buggy
    # result at another file version would commit and stamp flag 256 (measured on pylance 12.0.0).
    foreign = describe_foreign_data_file_versions(
        dataset.data_storage_version, [data_file for rewrite in rewrites for fragment in rewrite.new_fragments for data_file in fragment.files]
    )
    if foreign is not None:
        raise InvalidInputError(f"compaction result refused: {foreign}")
    try:
        metrics = lance_optimize.Compaction.commit(dataset, rewrites)
    except OSError as exc:
        raise _classify_commit_error(exc, remedy=_COMPACTION_REMEDY) from exc
    return CompactionOutcome(
        # THE HANDLE, not a second open. `Compaction.commit` advances the dataset in place — measured
        # against pylance 2026-09-07: a dataset at version 4 reports 6 on the same handle after the
        # commit, and `checkout_latest()` leaves it at 6. Re-opening from the URI to read this number
        # was a full manifest read over the object store, on the catalog's write path, for a value
        # already in memory.
        version=int(dataset.version),
        fragments_added=int(metrics.fragments_added),
        fragments_removed=int(metrics.fragments_removed),
        files_added=int(metrics.files_added),
        files_removed=int(metrics.files_removed),
    )


# Scalar Arrow type names → pyarrow factory, for the alter_columns re-type path. A ``JsonArrowDataType``
# carries a ``type`` name (+ optional ``fields``/``length`` for complex types); pylance's ``alter_columns``
# needs a real ``pa.DataType``, so we convert. Covers the documented re-types (e.g. float32→float16 on an
# embedding column); an unsupported/complex type raises a clear 400 instead of a silent Rust-boundary 500.
_SCALAR_ARROW: dict[str, Callable[[], pa.DataType]] = {
    "null": pa.null,
    "bool": pa.bool_,
    "boolean": pa.bool_,
    "int8": pa.int8,
    "int16": pa.int16,
    "int32": pa.int32,
    "int64": pa.int64,
    "uint8": pa.uint8,
    "uint16": pa.uint16,
    "uint32": pa.uint32,
    "uint64": pa.uint64,
    "float16": pa.float16,
    "halffloat": pa.float16,
    "float32": pa.float32,
    "float": pa.float32,
    "float64": pa.float64,
    "double": pa.float64,
    "string": pa.string,
    "utf8": pa.string,
    "large_string": pa.large_string,
    "binary": pa.binary,
    "large_binary": pa.large_binary,
    "date32": pa.date32,
    "date64": pa.date64,
}


def _json_arrow_to_pa_type(dt: dict[str, Any]) -> pa.DataType:
    """Convert a ``JsonArrowDataType`` dict (``{type, fields?, length?}``) to a ``pyarrow.DataType``.

    Handles scalars + fixed-size-list (vector embeddings). An unsupported/complex type raises
    ``InvalidInputError`` (400) rather than letting a dict reach pylance and fail as a 500 at the boundary.
    """
    name = str(dt.get("type") or "").lower()
    factory = _SCALAR_ARROW.get(name)
    if factory is not None:
        return factory()
    if name in ("fixed_size_list", "fixedsizelist"):
        fields, length = dt.get("fields") or [], dt.get("length")
        if fields and length:
            return pa.list_(_json_arrow_to_pa_type(fields[0].get("type") or {}), int(length))
    raise InvalidInputError(f"unsupported alter_columns data_type: {dt.get('type')!r}")


#: Lance prefixes a caller-expression failure with this, whatever exception class it picks.
_USER_INPUT_MARKER = "Invalid user input"
#: Lance appends the Rust source location that raised — ``, /home/runner/work/lance/lance/rust/…:150:14``.
#: Useful in a server log, noise in an API response, and it publishes the build path of a vendored library
#: to every caller. Trimmed from the detail; the log line still carries the original exception.
_RUST_SOURCE_SUFFIX = re.compile(r",\s*/\S+\.rs:\d+:\d+\s*\.?\s*$")


def clean_lance_message(message: str) -> str:
    """Lance's own text, minus the two things a caller must never receive.

    The ``Invalid user input`` prefix is replaced by our own action wording, and the Rust source location
    (``, /home/runner/work/lance/…/planner.rs:1019:20``) is trimmed: it is useful in a server log, noise in
    an API response, and it publishes the build path of a vendored library to every caller. Everything else
    survives — Lance frequently names the columns that DO exist, which is the whole value of the message.
    """
    body = message.split(_USER_INPUT_MARKER, 1)[1].lstrip(": ") if _USER_INPUT_MARKER in message else message
    return _RUST_SOURCE_SUFFIX.sub("", body).strip()


#: The most AND/OR connectives a caller's SQL fragment may carry. Lance's planner recurses once per
#: connective of a flat boolean chain with no bound: on pylance 12.0.0, 150,000 `id = n OR ...` terms end
#: the process with SIGSEGV. Its parser already bounds nesting ("recursion limit exceeded"), and a large
#: value set carries no connective as one `IN (...)` list, so the bound sits far below the crash and above
#: any hand-written filter.
MAX_CALLER_SQL_CONNECTIVES: Final = 1_000

_QUOTED_SQL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`")
_CONNECTIVE = re.compile(r"\b(?:and|or)\b", re.IGNORECASE)


def refuse_an_unbounded_boolean_chain(sql: str | None, *, field: str) -> None:
    """Refuse a caller's SQL fragment whose AND/OR chain could exhaust Lance's planner, before Lance sees it.

    Connectives inside quoted literals and identifiers are not counted: they are text, not structure.

    Raises:
        TypeError: ``sql`` is not a string.
        InvalidInputError: ``sql`` joins more than ``MAX_CALLER_SQL_CONNECTIVES`` conditions.
    """
    if sql is None:
        return
    if not isinstance(sql, str):
        raise TypeError(f"{field} must be a SQL string, got {type(sql).__name__}")
    connectives = len(_CONNECTIVE.findall(_QUOTED_SQL.sub(" ", sql)))
    if connectives > MAX_CALLER_SQL_CONNECTIVES:
        raise InvalidInputError(
            f"{field} joins {connectives} conditions with AND/OR; at most {MAX_CALLER_SQL_CONNECTIVES} are accepted — "
            "pass a large set of values as one IN (...) list"
        )


@contextmanager
def caller_sql(action: str) -> Iterator[None]:
    """Translate Lance's expression-parse failures into a 4xx instead of letting them escape as a 500.

    ``updates`` and ``predicate`` are SQL fragments the CALLER wrote, and Lance validates them at execution
    time — an unknown column or a syntax slip surfaces as ``Invalid user input: Schema error: No field named
    "Z". Valid fields are id, v.``. Uncaught, that became a 500 ``InternalError``, which is wrong in three
    ways: it tells the caller the server broke when their expression was wrong, it hides Lance's genuinely
    useful message (it lists the valid fields) behind a generic body, and it spends the error budget — a 500
    is what alerting watches — on a client mistake. Found live 2026-07-26 driving
    ``POST /v1/table/{id}/update`` with an unquoted string literal, which Lance reads as a column reference.

    Discriminates on Lance's ``Invalid user input`` MARKER, not on the exception class, because the class is
    not consistent across operations: the same bad predicate is a ``ValueError`` out of ``update()`` and an
    ``OSError`` out of ``delete()`` (both observed here, identical message). Keying on the type would have
    fixed update and left delete throwing 500s — and keying on ``except Exception`` would flip a genuine
    object-store outage into "fix your query". Anything without the marker propagates untouched.
    """
    try:
        yield
    except (ValueError, OSError) as exc:
        message = str(exc)
        if _USER_INPUT_MARKER not in message:
            raise  # a storage / IO failure: a real 5xx, and it stays one
        # Keep Lance's text — it names the columns that do exist — minus its prefix, which we replace, and
        # minus the Rust source location, which the caller can do nothing with.
        detail = clean_lance_message(message)
        log.info("user_sql_rejected", extra={"action": action, "error": message})
        raise InvalidInputError(f"{action}: {detail}") from exc


#: Lance's schema check on a write, raised as a bare `OSError`. A stable Rust error-variant prefix, not
#: a path: "Append with different schema: `s` should have type string but type was int64" for a type
#: mismatch, "…: fields did not match, missing=[], unexpected=[zz]" for a column the table lacks.
_WRITE_SCHEMA_MARKER = "append with different schema:"


@contextmanager
def _write_schema_errors() -> Iterator[None]:
    """Give the in-process write path the schema code the NATIVE path already answers.

    `insert_into_table` and `merge_insert_into_table` split on `branch`: without one they delegate to
    the native backend, which maps a schema mismatch to `TableSchemaValidationError` (20 -> 400); with
    one they run pylance in-process, where the same mismatch escapes as a bare `OSError` and is
    reported `Internal 18`. Measured 2026-09-07 across three payload shapes — wrong Arrow type, an
    extra column, a wholly unrelated schema — main answered 20 for all three and the branch answered
    500 for all three.

    So this is PARITY with a correct implementation next door, not a fresh judgement about which code
    a schema failure deserves: the branchless door already decided, and a caller must not get a
    different answer for the same mistake because they staged it on a branch.
    """
    try:
        yield
    except OSError as exc:
        if _WRITE_SCHEMA_MARKER not in str(exc).lower():
            raise  # a storage / IO failure: a real 5xx, and it stays one
        raise TableSchemaValidationError(clean_lance_message(str(exc))) from exc


#: A column op names a column that is not there. Lance says it four ways across three exception classes —
#: ``Column nope does not exist in the dataset`` (drop, ValueError), ``Invalid user input: Column "nope"
#: does not exist in the dataset`` (rename, OSError), ``Field 'nope' not found.\nAvailable fields: ['id']``
#: (update_field_metadata, OSError) — and the SPEC has a code for exactly this: 12 TableColumnNotFound (404).
#: Matched against pylance 9.0.0's real messages (probed, not guessed), so a pylance bump that rewords them
#: reds a test rather than silently reverting these paths to 500s.
_COLUMN_NOT_FOUND = re.compile(r"does not exist in the dataset|field '[^']*' not found", re.IGNORECASE)

#: Client mistakes Lance reports WITHOUT its ``Invalid user input`` marker. Each is a refusal of a
#: well-formed request the caller got wrong, i.e. 400 — never a server fault. The primary-key one is a
#: ``LanceError(Schema)`` OSError on pylance 12.0.0 for ``nullable=True`` on the key or an ancestor
#: ([[LH-208]]); it answered 500 Internal before this.
_COLUMN_BAD_REQUEST = re.compile(r"cannot drop all columns|primary key column and all its ancestors must not be nullable", re.IGNORECASE)

#: Messages that already enumerate the schema for the caller; appending our own list would just repeat it.
_LISTS_FIELDS = re.compile(r"valid fields|available fields", re.IGNORECASE)


@contextmanager
def _column_op(action: str, fields: Sequence[str] = ()) -> Iterator[None]:
    """Translate a schema-evolution failure the CALLER caused into the spec's 4xx, not a 500.

    The sibling of :func:`caller_sql` for the four column ops (add / alter / drop / update_field_metadata).
    It is a separate translator rather than a widening of ``caller_sql`` because ``caller_sql``'s single
    ``Invalid user input`` marker test is load-bearing for update/delete and would miss half of these:
    ``drop_columns`` raises an unmarked ``ValueError`` for both of its user errors. Seven distinct client
    mistakes answered 500 ``InternalError`` with detail "Internal Server Error" before this (#101) — the
    lakehouse UI renders that detail verbatim, so a user who typed a wrong column name read that the server
    had broken.

    Classification, in order:

    * ``LanceNamespaceError`` — already typed (``TableBranchNotFound`` out of ``open_dataset``, our own
      ``InvalidInput`` for an unsupported cast target): re-raised untouched.
    * a message naming a MISSING column → ``TableColumnNotFoundError`` (code 12 → 404). ``fields`` is the
      live schema, appended when Lance's own text does not already list it, so the answer always tells the
      caller both what was missing and what is there.
    * Lance's ``Invalid user input`` marker, or an unmarked message on the known-bad-request list →
      ``InvalidInputError`` (400).
    * anything else propagates — an object-store outage stays a 5xx instead of being blamed on the caller.
    """
    try:
        yield
    except LanceNamespaceError:
        raise
    except (ValueError, OSError) as exc:
        message = str(exc)
        detail = clean_lance_message(message)
        if _COLUMN_NOT_FOUND.search(message):
            if fields and not _LISTS_FIELDS.search(detail):
                detail = f"{detail}. Valid fields are {', '.join(fields)}"
            log.info("column_op_rejected", extra={"action": action, "error": message})
            raise TableColumnNotFoundError(f"{action}: {detail}") from exc
        verdict = classify_commit_failure(exc)
        if verdict is CommitVerdict.RETRYABLE_CONFLICT:
            # A LOST RACE, NOT A BAD REQUEST — and the difference is what the caller should do next.
            # Measured 2026-09-07: six concurrent `add_columns` on one table, five lose with
            # `OSError("Retryable commit conflict for version 2: This Merge transaction was preempted by
            # concurrent transaction ...")`. Reported as Internal 18 that reads as "the server broke", so
            # a client retries nothing and an operator is paged for contention that resolves itself.
            # Code 14 says re-read and re-commit, which is exactly what Lance calls it: retryable.
            #
            # AFTER the missing-column test on purpose: the conflict vocabulary carries the bare word
            # `concurrent`, so a column actually NAMED `concurrent` would match here — it is caught above
            # as the 12 it really is, and a genuine conflict never looks like a missing column (both
            # directions verified against the two regexes).
            raise ConcurrentModificationError(f"{action}: {detail} — re-read the table version and retry") from exc
        if verdict is CommitVerdict.INCOMPATIBLE:
            # The OTHER conflict, which this guard could not see while it matched a flat tuple: the table
            # was REPLACED underneath the op, so retrying is what corrupts it. Sharing the ordered verdict
            # is what makes the distinction reach every door at once rather than one at a time.
            raise InvalidInputError(
                f"{action}: {detail} — this is NOT retryable: the table changed underneath it (e.g. a concurrent overwrite). "
                "Re-read the current version and re-plan; do not retry this operation"
            ) from exc
        if _USER_INPUT_MARKER in message or _COLUMN_BAD_REQUEST.search(message):
            log.info("column_op_rejected", extra={"action": action, "error": message})
            raise InvalidInputError(f"{action}: {detail}") from exc
        raise  # not attributable to the request: a real 5xx, and it stays one


def update_table(ns: LanceNamespace, so: StorageOptions, req: UpdateTableRequest) -> UpdateTableResponse:
    """Apply SQL ``[path, expression]`` updates to matching rows."""
    table_id = _table_id(req)
    updates = dict(req.updates or [])
    if not updates:
        raise InvalidInputError("update requires at least one [path, expression] pair")
    # `branch=` is load-bearing and was MISSING. The request carries it, the served OpenAPI advertises
    # it, and every sibling mutation on this object passes it — so a caller working on a branch mutated
    # MAIN, got a 200, and the lineage WROTE edge recorded main's version. A branch exists precisely so
    # work can be staged without touching main; silently writing to main is the one outcome that makes
    # the feature worse than not having it.
    refuse_an_unbounded_boolean_chain(req.predicate, field="predicate")
    for expression in updates.values():
        refuse_an_unbounded_boolean_chain(expression, field="updates")
    dataset = open_dataset(ns, so, table_id, branch=req.branch)
    with caller_sql("invalid update expression or predicate"):
        result = dataset.update(updates, where=req.predicate)
    # pylance's update() returns an UpdateResult TypedDict (a plain dict at runtime); the row count is
    # `num_rows_updated`. (The previous `getattr(result, "num_updated_rows", ...)` was wrong twice — attr
    # access on a dict + wrong key — so updated_rows was hard-wired to 0 on every successful update.)
    updated = result.get("num_rows_updated") if isinstance(result, dict) else None
    # The version off the SAME handle the update committed through — pylance advances it in place.
    return UpdateTableResponse(updated_rows=updated if updated is not None else 0, version=dataset.version)


def delete_from_table(ns: LanceNamespace, so: StorageOptions, req: DeleteFromTableRequest) -> DeleteFromTableResponse:
    """Delete rows matching the request predicate."""
    table_id = _table_id(req)
    # Same omission as `update_table` above, and worse here: a wrong-target DELETE loses rows that were
    # never meant to be touched, and returns 200.
    refuse_an_unbounded_boolean_chain(req.predicate, field="predicate")
    dataset = open_dataset(ns, so, table_id, branch=req.branch)
    with caller_sql("invalid delete predicate"):
        dataset.delete(req.predicate)
    return DeleteFromTableResponse(version=dataset.version)


def insert_into_table(ns: LanceNamespace, so: StorageOptions, req: InsertIntoTableRequest, data: bytes, *, max_bytes: int) -> InsertIntoTableResponse:
    """Append (or overwrite) Arrow-IPC rows on the ref the request NAMES, and report the commit it made.

    ONE PATH FOR EVERY REF, through a handle opened on that ref. A branch needs it: measured live
    2026-08-31, the upstream op answered `?branch=work` by appending to main. Main needs it too, for the
    version: the `dir` backend's `insert_into_table` answers `{}` (measured on pylance 12.0.0), while the
    spec's `InsertIntoTableResponse.version` is "the commit version associated with the operation". A
    reopen cannot recover that number: under concurrent appends it reports whichever commit landed last.
    `LanceDataset.insert` advances its own handle to the version IT committed, after any conflict
    rebase (measured on 12.0.0: two handles appending in turn report v3 and v4, each holding its own
    rows), so the response and the lineage event name the write that happened.

    The handle is opened through the checked open ([[LH-279]]), so the manifest the write builds on is
    the one judged. `mode` is read through `InsertMode`, whose two values are pylance's own spellings of
    the spec's two modes.
    """
    # Validated here as well as at the door's coercion: this hands pyarrow's buffers to Lance in-process,
    # and Lance writes whatever an unvalidated offset points at.
    rows = read_arrow_body(data, max_bytes=max_bytes)
    dataset = open_dataset(ns, so, _table_id(req), branch=req.branch)
    # THROUGH `InsertMode`, the one vocabulary every caller shares. pylance's own parser is not the
    # spec's: it takes `create`, which the spec does not give this door, and refuses an unknown value with
    # a bare ValueError that would answer 500. The parse is idempotent, so a mode the door already parsed
    # passes through unchanged.
    with _write_schema_errors():
        dataset.insert(rows, mode=InsertMode.parse(req.mode).value)
    # The rows this request wrote, read off its own payload: a row count diff across the commit would
    # include whatever a concurrent writer appended in between.
    return InsertIntoTableResponse(version=int(dataset.version), num_inserted_rows=rows.num_rows)


def merge_insert_into_table(
    ns: LanceNamespace, so: StorageOptions, req: MergeInsertIntoTableRequest, data: bytes, *, max_bytes: int
) -> MergeInsertIntoTableResponse:
    """Run the spec's merge-insert against the ref the request NAMES.

    Same defect and same severity as `insert_into_table`: verified live, a merge naming `work` applied
    its update to MAIN and left the branch untouched, reporting `num_updated_rows: 1` for a row it had
    not changed on the dataset the caller asked about.

    Every spec parameter has a `MergeInsertBuilder` method of the same name, so the mapping is
    transcription rather than interpretation — which is exactly why this door is HONOURED where `query`
    is refused. There is no vector search, no full-text query and no index-selection surface here to
    re-derive and get subtly wrong.
    """
    # BEFORE EITHER ARM. The branch arm hands pyarrow's buffers to Lance in-process, where an offset
    # past its values buffer is written as process heap and a decreasing one crashes the process
    # (measured on pylance 12.0.0); main's native reader refuses both, as a 500. Checked once, here, so
    # both arms answer 400.
    refuse_an_unbounded_boolean_chain(req.when_matched_update_all_filt, field="when_matched_update_all_filt")
    refuse_an_unbounded_boolean_chain(req.when_not_matched_by_source_delete_filt, field="when_not_matched_by_source_delete_filt")
    rows = read_arrow_body(data, max_bytes=max_bytes)
    if req.branch is None:
        judged_native_version(ns, so, _table_id(req), version=None)
        return cast(MergeInsertIntoTableResponse, native.call(ns, "merge_insert_into_table", req, data))
    dataset = open_dataset(ns, so, _table_id(req), branch=req.branch)
    # THE BUILDER IS CONSTRUCTED INSIDE THE GUARD, and that placement is the fix rather than a tidy-up:
    # `merge_insert(on)` is where Lance rejects a key column that does not exist, and it sat outside
    # `caller_sql`, so the one door whose whole job is matching on that column reported `Internal 18`
    # for naming it wrongly — while the branchless path answered 13.
    with _write_schema_errors(), caller_sql("invalid merge_insert filter"):
        builder = dataset.merge_insert(req.on)
        if req.when_matched_update_all:
            builder.when_matched_update_all(req.when_matched_update_all_filt)
        if req.when_not_matched_insert_all:
            builder.when_not_matched_insert_all()
        if req.when_not_matched_by_source_delete:
            builder.when_not_matched_by_source_delete(req.when_not_matched_by_source_delete_filt)
        if req.use_index is not None:
            builder.use_index(req.use_index)
        stats = builder.execute(rows)
    counts = stats if isinstance(stats, dict) else {}
    return MergeInsertIntoTableResponse(
        version=dataset.version,
        num_updated_rows=int(counts.get("num_updated_rows", 0)),
        num_inserted_rows=int(counts.get("num_inserted_rows", 0)),
        num_deleted_rows=int(counts.get("num_deleted_rows", 0)),
    )


def refuse_a_branch_this_door_cannot_honour(branch: str | None, *, door: str, remedy: str | None = None) -> None:
    """Refuse a branch-scoped read the upstream implementation answers from MAIN.

    `query_table`, `explain_table_query_plan` and `analyze_table_query_plan` all declare `branch` and
    all delegate whole-request to the upstream implementation, which ignores it. Verified live
    2026-08-31: after deleting two of three rows on `work`, `/query` returned 3 rows for `branch=work`
    AND 3 for a branch that had never been created — main's answer, twice, with a 200.

    A 501 rather than a wrong 200, and that IS the fix at this door rather than a deferral of it. The
    defect is not "branch queries are missing"; it is that the parameter is accepted and disregarded, so
    a caller staging work on a branch reads main and is told nothing. Answering "this backend does not
    do that" is correct, complete and spec-shaped — error code 0, `Unsupported` — where answering with
    another dataset's rows is neither.

    THE INDEX DOORS ARE HERE TOO, and they are WRITES, which makes the refusal more urgent rather than
    less. Measured live 2026-08-31: `create_scalar_index` with `branch=work` returned 200, MAIN advanced
    a version and took the index, and the branch got none. An index on the wrong dataset is not inert —
    it changes which plans the query engine picks for every later reader of main.

    NOT the same call as `count_rows`, which is honoured rather than refused: a count is a single
    documented operation over a dataset handle, so serving it branch-aware adds no second definition of
    anything. A faithful branch-scoped `query` would have to re-derive vector search, full-text search,
    prefilter, nprobes, refine_factor and distance_type against a handle, and a subtle divergence there
    is a wrong answer wearing the right shape — the failure this whole file exists to stop. The index
    doors sit on the same side of that line for the same reason: `CreateTableIndexRequest` carries a
    whole full-text-search option surface (`base_tokenizer`, `language`, `stem`, `ascii_folding`,
    `remove_stop_words`, `with_position`, `max_token_length`, `lower_case`) plus vector parameters, and
    silently building an index with a DEFAULT where the caller asked for a setting is the same class of
    quiet wrongness as building it on the wrong dataset.

    `ensure_merge_key_index` is deliberately NOT refused, and the distinction is the option surface, not
    the operation: it builds one fixed shape — a BTREE on the merge key, no user options — so it can be
    served through the handle with nothing left to get wrong.

    Both implementations are real work with their own tests; they are named in the lakehouse register, row O1 (drained 2026-09-10; in git history), not
    smuggled in behind a door that currently lies.
    """
    if branch is not None:
        # ONE EXPRESSION DECIDES THE CODE, and `remedy` exists so that staying inside it costs a door
        # nothing. `describe_table` refused this same condition inline with `InvalidInputError` (13)
        # while fifteen doors answered `UnsupportedOperationError` (0) — the spec calls 13 "Malformed
        # request or invalid parameters" (`spec.yaml:2425`) and 0 "Operation not supported by this
        # backend" (`:2412`), and a well-formed branch name this backend does not serve is the second.
        # A client dispatching on the 24 codes cannot tell two answers apart as one condition, so the
        # divergence was a contract defect rather than a wording one. Doors differ in what the caller
        # should do INSTEAD; they must not differ in what kind of thing happened.
        raise UnsupportedOperationError(
            f"{door} cannot be scoped to a branch {branch!r}: the underlying implementation answers from the main branch regardless of "
            f"`branch`, so honouring the parameter here would return main's rows labelled as {branch!r}. "
            + (remedy or "Read the branch through `count_rows`, or query it directly.")
        )


def count_rows(ns: LanceNamespace, so: StorageOptions, req: CountTableRowsRequest) -> int:
    """Count rows on the ref the request NAMES — the read-side twin of `update_table`'s omission.

    The upstream `count_table_rows` takes the whole request and answers from main whatever `branch`
    says, so a branch-scoped count returned a number that was correct for a dataset the caller did not
    ask about. Verified live 2026-08-31 against ground truth read from S3 with pylance: after deleting
    two of three rows on `work`, the branch held 1 and this door reported 3 — and a branch that had
    NEVER BEEN CREATED also reported 3, which is what proves the parameter was not read rather than
    mishandled.

    Spec-mandated on both counts. `CountTableRowsRequest.branch` is "Branch to target. When not
    specified, the main branch is used" (namespace.md), and the SDK states the general rule for a
    branch-scoped handle: "Reads and writes operate in the branch's context." An absent ref is error
    code 22 `TableBranchNotFound`, which `open_dataset` already raises — so routing through it buys the
    404 as well as the correct answer.

    MAIN STAYS ON THE UPSTREAM PATH. A branchless count is the overwhelmingly common one and already
    correct; re-implementing it here would put a second definition of "count" in the estate for no
    defect, and any divergence in predicate dialect or version resolution would be ours to own.
    """
    refuse_an_unbounded_boolean_chain(req.predicate, field="predicate")
    if req.branch is None:
        # PINNED TO THE VERSION JUDGED ([[LH-279]]): the native count opens the table inside Rust, so the
        # request carries the exact version whose bases were just checked.
        req.version = judged_native_version(ns, so, _table_id(req), version=req.version)
        response = native.call(ns, "count_table_rows", req)
        if not isinstance(response, CountTableRowsResponse) or response.count is None:
            raise TypeError(f"count_table_rows must answer a CountTableRowsResponse with a count, got {type(response).__name__}: {response!r}")
        return response.count
    dataset = open_dataset(ns, so, _table_id(req), version=req.version, branch=req.branch)
    with caller_sql("invalid count predicate"):
        return int(dataset.count_rows(filter=req.predicate) if req.predicate else dataset.count_rows())


def add_columns(ns: LanceNamespace, so: StorageOptions, req: AlterTableAddColumnsRequest) -> AlterTableAddColumnsResponse:
    """Add columns computed from per-column SQL expressions."""
    table_id = _table_id(req)
    columns = req.new_columns or []
    # The spec also allows a `virtual_column` (UDF/Docker-backed) instead of a SQL `expression`. That needs
    # a UDF execution backend we don't run, so reject it as a spec-correct 501 — not a 400 (it's a valid
    # request for an unsupported feature, not invalid input).
    if any(getattr(c, "virtual_column", None) is not None and not c.expression for c in columns):
        raise UnsupportedOperationError("virtual_column add_columns is not supported by this backend")
    # `computed` arrived with lance-namespace 0.11.0 (PR #360) as a THIRD way to declare a column: the
    # column is added all-null with the SQL expression persisted as a binding in field metadata, and rows
    # are filled later by `alter_table_backfill_columns` — never at declaration. That is a different
    # operation from the immediate `expression` transform below, and the `dir` backend implements neither
    # the binding nor the backfill.
    #
    # Without this arm a spec-VALID 0.11.0 request (`{"name": "x", "computed": "a + b"}`) fell past the
    # virtual_column guard, produced an empty `transforms`, and hit the InvalidInput below — a 400 saying
    # the caller had sent no SQL expression when they had sent one the server simply does not support.
    # 501 is the spec's answer for a valid request naming an unsupported feature.
    #
    # `getattr` so this file works against 0.9.0 and 0.11.0 alike, exactly as the virtual_column guard
    # above already does — the field does not exist on the older model.
    if any(getattr(c, "computed", None) is not None and not c.expression for c in columns):
        raise UnsupportedOperationError(
            "computed add_columns is not supported by this backend — the expression binding and its backfill are both unimplemented for the dir backend"
        )
    transforms = {c.name: c.expression for c in columns if c.expression}
    if not transforms:
        raise InvalidInputError("add_columns requires a name and SQL expression per column")
    dataset = open_dataset(ns, so, table_id, branch=req.branch)
    with _column_op("add_columns", dataset.schema.names):
        dataset.add_columns(transforms)
    return AlterTableAddColumnsResponse(version=dataset.version)


def alter_columns(ns: LanceNamespace, so: StorageOptions, req: AlterTableAlterColumnsRequest) -> AlterTableAlterColumnsResponse:
    """Rename / re-type / change nullability of existing columns."""
    table_id = _table_id(req)
    alterations: list[dict[str, object]] = []
    for entry in req.alterations or []:
        alteration: dict[str, object] = {"path": entry.path}
        if entry.rename is not None:
            alteration["name"] = entry.rename
        if entry.nullable is not None:
            alteration["nullable"] = entry.nullable
        dt = getattr(entry, "data_type", None)
        if dt is not None:
            # data_type is a JsonArrowDataType dict; pylance needs a real pa.DataType, not the JSON dict.
            alteration["data_type"] = _json_arrow_to_pa_type(dt if isinstance(dt, dict) else dt.model_dump())
        alterations.append(alteration)
    dataset = open_dataset(ns, so, table_id, branch=req.branch)
    provenance_guard.refuse_provenance_alter(dataset.schema, alterations)
    with _column_op("alter_columns", dataset.schema.names):
        # pylance accepts plain dict alterations at runtime; its stub types them as
        # AlterColumn (a TypedDict), which ty can't match from dict[str, object].
        dataset.alter_columns(*alterations)  # ty: ignore[invalid-argument-type]
    return AlterTableAlterColumnsResponse(version=dataset.version)


def drop_columns(ns: LanceNamespace, so: StorageOptions, req: AlterTableDropColumnsRequest) -> AlterTableDropColumnsResponse:
    """Drop the named columns from the table."""
    table_id = _table_id(req)
    columns = list(req.columns or [])
    dataset = open_dataset(ns, so, table_id, branch=req.branch)
    provenance_guard.refuse_provenance_drop(dataset.schema, columns)
    with _column_op("drop_columns", dataset.schema.names):
        dataset.drop_columns(columns)
    return AlterTableDropColumnsResponse(version=dataset.version)


class _ChunkSink(io.RawIOBase):
    """A write target that holds only what the IPC writer has emitted since the last drain.

    ON THE DOCUMENTED PATH, not a workaround: Arrow's IPC guide states the record-batch writers
    "can write to a writeable ``NativeFile`` object **or a writeable Python object**", and
    `pa.output_stream`'s own signature takes ``source : str, Path, buffer, file-like object``. A
    Python sink is first-class API, so `pa.ipc.new_file` takes this directly with no wrapper.

    It exists because the obvious sink cannot stream. `pa.BufferOutputStream` accumulates every byte
    until `getvalue()` — which is the one-shot shape, and the shape every FastAPI+Arrow example
    reaches for (`BytesIO` + `RecordBatchFileWriter` + `Response`). That is correct for a small
    answer and is precisely what costs 2.5x the payload on a large one. Draining after each batch
    hands the bytes out and forgets them, so what is held is one batch rather than the whole table.

    `io.RawIOBase` rather than a bare class with `write`: it supplies `writable()`, `closed` and the
    context-manager protocol that pyarrow's `PythonFile` probes for, without restating them.
    """

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, b: Any) -> int:  # noqa: ANN401 — pyarrow writes bytes, memoryview or bytearray
        payload = bytes(b)
        self._parts.append(payload)
        return len(payload)

    def drain(self) -> bytes:
        """Everything written since the last call, and nothing after it."""
        out = b"".join(self._parts)
        self._parts.clear()
        return out


def _arrow_file_chunks(schema: pa.Schema, first: pa.RecordBatch | None, rest: Iterator[pa.RecordBatch]) -> Iterator[bytes]:
    """One Arrow FILE, yielded a batch at a time — byte-identical to the one-shot form.

    Byte-identical is verified, not assumed (2026-09-22, 58,410,370 bytes from both paths over the
    same projection, matching schema and rows). It matters because it is what makes this a pure
    memory change: no consumer can tell the two apart, so nothing downstream has to be migrated.

    FILE framing rather than STREAM is not a detail to trade away for streamability: `/query` answers
    `application/vnd.apache.arrow.file` and a consumer handed the other framing fails at the first
    batch. The footer that makes it a FILE is written by `new_file.__exit__`, so it arrives as the
    last drain — which is exactly why the close happens inside this generator rather than before it.

    ``first`` is passed in already pulled: its caller needs the pull to happen under its own error
    guard, and re-deriving it here would either lose that batch or scan twice.
    """
    sink = _ChunkSink()
    # `first is None` means the scan was empty, so `rest` is exhausted too and chaining it in would
    # hand `write_batch` a None. The file is still written: an empty FILE is a schema and a footer,
    # which is what a consumer asking a window nothing changed in must receive.
    batches = rest if first is None else itertools.chain((first,), rest)
    with pa.ipc.new_file(sink, schema) as writer:
        for batch in batches:
            writer.write_batch(batch)
            if chunk := sink.drain():
                yield chunk
    if chunk := sink.drain():
        yield chunk


def read_changes(
    ns: LanceNamespace,
    so: StorageOptions,
    table_id: list[str],
    *,
    predicate: str,
    columns: list[str] | None = None,
    branch: str | None = None,
) -> Iterator[bytes]:
    """Rows matching a change-feed predicate, Arrow FILE-framed (§ J4), handed out in pieces.

    Its own scan rather than the query door's, because `QueryTableRequest` is a VECTOR model — `k` and
    `vector` are required — so reusing it would mean inventing a vector to ask a question that has
    nothing to do with similarity.

    FILE FRAMING, not stream: this answers `application/vnd.apache.arrow.file`, matching the query
    door, and a consumer reading one framing as the other fails at the first batch rather than
    degrading. `encode_arrow_stream` is the sibling for the stream doors and is deliberately not reused.

    The predicate AND the projection come from `services/changes.py`, which owns the two documented
    windows; this function does not know what a change is and must not learn — a second place that
    composes either is a second place it can drift from `file_format.md`.

    THE PROJECTION IS PART OF THE ANSWER, not a default: Lance returns only data columns unless the
    version pseudo-columns are named, so a feed that passed the caller's `columns` through verbatim
    handed back changed rows with no version on them and no way to ask for the next window.

    IT YIELDS rather than returns, because the answer is sized by the DATA and this door has no bound
    to put on it — no `limit`, no page token, and none available: the version window is the only
    cursor a consumer has, and one version can carry the whole table.

    MEASURED 2026-09-22 (200k rows x 256B, 58.4 MB of Arrow over the feed's five projected columns),
    same projection on both sides: the materialising form peaked at **147.6 MB of RSS for one call,
    2.53x the payload**, because it held three copies at once — the scan's table, the IPC encoding
    beside it, and `to_pybytes()` copying that onto the Python heap. Yielding peaks at **60.1 MB,
    1.03x**, and the wire output is byte-identical (58,410,370 both), same schema, same rows.

    RSS is the measurement because nothing cheaper can see this: `tracemalloc` reports 0 MB for the
    scan and the encode, watching the Python heap while Arrow allocates elsewhere — the blind spot
    [[LH-183]] paid for twice.
    """
    dataset = open_dataset(ns, so, table_id, branch=branch)
    projection = changes.feed_projection(columns, data_columns=dataset.schema.names)
    with caller_sql("invalid change-feed predicate"):
        scanner = dataset.scanner(filter=predicate, columns=projection)
        # THE FIRST BATCH IS PULLED INSIDE THE GUARD, and that placement is the whole reason this is
        # not one line. `to_batches()` is lazy, so a malformed predicate does not raise until the
        # first pull — and a generator's body does not run until the response is already streaming,
        # where a raise becomes a truncated 200 instead of the 400 `caller_sql` exists to produce.
        batches = scanner.to_batches()
        first = next(batches, None)
    return _arrow_file_chunks(scanner.projected_schema, first, batches)


def read_deleted_row_ids(
    ns: LanceNamespace,
    so: StorageOptions,
    table_id: list[str],
    *,
    begin_version: int,
    end_version: int | None = None,
    branch: str | None = None,
) -> Iterator[bytes]:
    """The `_rowid`s deleted in ``(begin_version, end_version]``, Arrow FILE-framed (§ J4, LH-008).

    ITS OWN DOOR BECAUSE IT IS ITS OWN QUESTION. `read_changes` scans the table with a predicate over
    the version columns, and those columns describe rows the table STILL HAS — a deleted row is absent
    from every scan, so no filter can name it. Lance answers from the TRANSACTION range instead:
    `DatasetDelta.get_deleted_row_ids()` streams a single `_rowid` column (verified against the
    installed pylance; the method documents "Requires stable row ids", which the catalog's creation
    contract already enforces on every governed dataset).

    WHY THE FEED NEEDS IT AT ALL: the publication delta is insert-only while the cascade's writer
    hard-deletes with `when_not_matched_by_source_delete`, so a retracted row left the tier without a
    trace any consumer could follow and silver and gold served it indefinitely.

    SERVED ON DEMAND rather than stamped into the publish event (owner decision, 2026-09-11): a
    deleted-row set is unbounded, so stamping it turns a large delete into a large message on the bus.
    Publishing a version range and letting the consumer pull is what every change-data system of this
    shape does.

    ONLY `_rowid`, and that is Lance's answer rather than a projection choice — the rows are gone, so
    there is nothing else left to return. A consumer resolves them against whatever it stored when it
    read the row.

    THE WINDOW IS ALWAYS CLOSED, because `delta()` refuses an open one — measured against the installed
    pylance: `end_version=None` raises "Must specify both with_begin_version and with_end_version"
    (`lance/src/dataset/delta.rs`). The scan doors accept "everything since", so an omitted
    `end_version` is closed HERE at the version of the dataset this call opened.
    That is exact rather than a separately-read bound: it is the same handle the delta is read from, so
    there is no moment between the two in which a write could land — which is the hazard
    `changes.change_filter` refuses to take by defaulting a bound it would have to read separately.
    """
    dataset = open_dataset(ns, so, table_id, branch=branch)
    with caller_sql("invalid deleted-row window"):
        reader = dataset.delta(begin_version=begin_version, end_version=end_version if end_version is not None else dataset.version).get_deleted_row_ids()
        # Streamed for the same reason `read_changes` is, and the count is no smaller for being one
        # column: a bulk delete names every row it removed, so `read_all()` sized this answer by the
        # DELETION and then copied it twice more on the way out.
        schema = reader.schema
        batches = iter(reader)
        first = next(batches, None)
    return _arrow_file_chunks(schema, first, batches)


def filter_internal_metadata(metadata: dict[str, str]) -> dict[str, str]:
    """Drop the platform's reserved keys (`provenance_guard.RESERVED_METADATA_PREFIXES`) from a map a caller will see.

    They are the coordinates that make the Lance file self-describing, not user properties. The map a
    caller holds is the map it saves back, and every write door refuses a reserved key, so handing them
    out would hand the caller a map it cannot save.
    """
    return {k: v for k, v in metadata.items() if not provenance_guard.is_reserved_key(k)}


def read_schema_metadata(ns: LanceNamespace, so: StorageOptions, table_id: list[str]) -> dict[str, str]:
    """The table's schema-level metadata as ``{str: str}`` — the read twin of ``schema_metadata/update``.

    pylance 8.0.0's ``describe_table`` leaves ``DescribeTableResponse.metadata`` unpopulated, so the #74
    Table Properties UI could WRITE a property but never SEE it back (the editor always seeded empty — found
    by driving the real UI in a browser, 2026-07-21). The catalog fills the gap: read ``schema.metadata``
    (Arrow bytes keys/values → str) directly. The reserved ``lineage.*`` / ``rask.*`` keys are excluded — they're
    not user properties, and the update path MERGES, so hiding them never drops them on a UI save.
    """
    meta = open_dataset_unchecked(ns, so, table_id).schema.metadata or {}
    out: dict[str, str] = {}
    for k, v in meta.items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        out[key] = v.decode() if isinstance(v, bytes) else str(v)
    return filter_internal_metadata(out)


def update_schema_metadata(
    ns: LanceNamespace, so: StorageOptions, table_id: list[str], values: dict[str, str | None], *, branch: str | None = None
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
    reports, and the version the update committed: the handle advances to it in place (measured on pylance
    12.0.0), so the lineage event names this commit rather than whatever a reopen finds.
    """
    dataset = open_dataset(ns, so, table_id, branch=branch)
    result = dataset.update_schema_metadata(values)
    return filter_internal_metadata(result), int(dataset.version)


def coerce_insert_arrow(
    ns: LanceNamespace, so: StorageOptions, table_id: list[str], data: bytes, branch: str | None = None, *, max_bytes: int, overwrite: bool = False
) -> bytes:
    """Align Arrow-IPC insert rows to the table's schema before the append.

    A client that INFERS types loosely — most importantly the browser's apache-arrow, which infers
    ``float64`` for every JS number — otherwise hits the ``insert_into_table`` append with a ``float64``
    batch against an ``int64`` column, which raises a bare **500** ("Internal Server Error"). That is both
    a broken insert AND the wrong status for a client-side mismatch (browser-driven find 2026-07-21). Here we
    select the table's columns BY NAME (a column the table lacks is dropped and LOGGED by name
    -- `insert_dropped_columns`; a missing one is a 400) and cast each to its
    real column type — so ``4.0 → 4`` just works — turning a genuinely incompatible payload (e.g. ``4.5`` →
    ``int``, or a non-castable type) into a clean ``400``, never a 500. Blocking IO (opens the dataset for the
    live schema); the caller runs it in a threadpool.

    An already-aligned payload passes through UNTOUCHED: re-serializing it cost two full
    materialisations (the IPC re-encode + ``to_pybytes``) on every insert from a schema-exact client —
    which is every non-browser client — for zero behavioural difference (#141).

    The body is read through :func:`read_arrow_body`, so one that is not a valid Arrow IPC stream is
    refused 400 before the dataset opens, on main and on a branch alike. Rows that would be over
    ``max_bytes`` as the table's types are refused before the cast runs (:mod:`catalog.services.cast_size`),
    so a dictionary decoded once per row cannot allocate past the cap, and the append is never handed a body
    larger than the one the caller was held to.

    AN ``overwrite`` KEEPS THE TABLE'S PROVENANCE ([[LH-208]]). Lance takes an overwrite's schema from
    its payload, metadata included, so a payload naming a reserved key would forge one and a payload
    with none would erase every ``lineage.*`` stamp and the primary key's field metadata (measured on
    pylance 12.0.0, on main and on a branch). A reserved key in the payload is refused, and the body handed on carries
    the table's own schema and field metadata. An append is left alone: Lance discards an append
    payload's schema metadata (measured on the same version).
    """
    incoming = read_arrow_body(data, max_bytes=max_bytes)
    if overwrite:
        provenance_guard.refuse_reserved_keys((incoming.schema.metadata or {}).keys(), door="insert?mode=overwrite payload")
    # THE REF THE REQUEST NAMES, never main. This alignment DROPS columns the target does not have, so
    # aligning a branch-targeted insert to main's schema silently deletes any column the branch has and
    # main does not — and then the insert succeeds, reporting rows it quietly rewrote. A branch whose
    # schema has evolved is the whole reason a branch exists, so this is the ordinary case rather than a
    # corner. Same defect family as `update`/`delete` rewriting main
    # (`test_branch_scoped_mutations_hit_the_branch.py`); it survived here because the door-level gate
    # walks doors that build a branched request model, and this is a helper.
    target = open_dataset(ns, so, table_id, branch=branch).schema
    if [(f.name, f.type, f.nullable) for f in incoming.schema] == [(f.name, f.type, f.nullable) for f in target]:
        if not overwrite or incoming.schema.equals(target, check_metadata=True):
            return data
        body = encode_arrow_stream(pa.Table.from_arrays(incoming.columns, schema=target))
        _refuse_over_the_cap(len(body), max_bytes)
        return body
    present = set(incoming.column_names)
    missing = [f.name for f in target if f.name not in present]
    if missing:
        raise InvalidInputError(f"insert rows are missing column(s): {', '.join(missing)}")
    discarded = sorted(present - {f.name for f in target})
    if discarded:
        # DROPPING IS NOT THE DEFECT, SILENCE IS. The table has no such column, so there is nowhere to
        # put these values and the only choices are refuse or discard; discarding is what keeps the
        # browser client this coercion exists for working. But a caller that is never told cannot tell
        # a stored value from a lost one, and the sibling above already raises for the other half of
        # the same schema mismatch. Reported, not refused: accept -> refuse on a live door is a
        # contract change rather than a bug fix.
        log.warning("insert_dropped_columns", extra={"table": table_id, "columns": discarded})
    selected = pa.table({f.name: incoming.column(f.name) for f in target})
    _refuse_over_the_cap(sum(bytes_after_cast(selected.column(f.name), f.type) for f in target), max_bytes)
    try:
        aligned = selected.cast(target if overwrite else pa.schema([pa.field(f.name, f.type, f.nullable) for f in target]))
    except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError) as exc:
        raise InvalidInputError(f"insert rows don't match the table schema: {exc}") from exc
    body = encode_arrow_stream(aligned)
    _refuse_over_the_cap(len(body), max_bytes)
    return body


def _refuse_over_the_cap(size: int, max_bytes: int) -> None:
    """Both arms of the insert door read the coerced body again, so it is held to the caller's cap here, once."""
    if size > max_bytes:
        raise InvalidInputError(
            f"the insert rows take {size:,} bytes as the table's column types, over the {max_bytes:,}-byte limit on a body;"
            " send them as those types, or in smaller inserts"
        )


def update_field_metadata(
    ns: LanceNamespace,
    so: StorageOptions,
    table_id: list[str],
    updates: list[dict[str, Any]],
    branch: str | None = None,
) -> UpdateFieldMetadataResponse:
    """Merge/replace per-field metadata for the given field paths."""
    field_updates = {u["path"]: dict(u.get("metadata") or {}) for u in updates if u.get("path")}
    replace = any(bool(u.get("replace")) for u in updates)
    dataset = open_dataset(ns, so, table_id, branch=branch)
    with _column_op("update_field_metadata", dataset.schema.names):
        dataset.update_field_metadata(field_updates, replace=replace)
    # A None value is the key-deletion signal for the backend; drop those from the
    # echoed map since the response model's field values are non-nullable strings.
    fields = {path: {k: v for k, v in meta.items() if v is not None} for path, meta in field_updates.items()}
    return UpdateFieldMetadataResponse(version=dataset.version, fields=fields)


#: Per-operation fields worth surfacing in a commit log, keyed by the pylance operation class name. Read
#: off the real objects rather than guessed — probed against pylance 8.0.0 with a create → append → delete
#: → update sequence, which is where the shape of each of these came from:
#:
#:   Overwrite  fragments, new_schema           (a create: "the table came into being with these columns")
#:   Append     fragments                       ("N fragments arrived")
#:   Delete     predicate, updated_fragments,   ("rows matching `id = 2` went away") — the predicate is the
#:              deleted_fragment_ids             single most useful field in the whole log
#:   Update     update_mode, fields_modified,   ("these fields were rewritten, this way")
#:              updated_fragments, new_fragments
#:
#:   Restore    version                          ("this version's content is version N's") — see the rename
#:              below; the field name collides with the row's own key
#:   CreateIndex new_indices, removed_indices     ("an index was built/dropped here")
#:   Rewrite    groups, rewritten_indices         (compaction: "14 groups were rewritten")
#:   Project    schema                            (drop_columns)
#:   Merge      fragments, schema                 (add_columns)
#:
#: Anything not listed but MODELLED still gets its operation NAME, so a Merge/Compact/CreateIndex commit
#: appears in the log as itself rather than vanishing — an operation missing from this table is a gap in
#: the table, never a gap in the history. An operation pylance models no subclass for cannot be named at
#: all and reports a null operation, like a transaction that cannot be read.
#:
#: Verified against pylance 8.0.0 by reading ``__dataclass_fields__`` off each ``lance.LanceOperation``
#: class rather than trusting the docs, because the whole value of this endpoint is reporting what Lance
#: actually recorded.
_TXN_DETAIL_FIELDS = (
    "predicate",
    "update_mode",
    "fields_modified",
    "updated_fragments",
    "new_fragments",
    "removed_fragment_ids",
    "deleted_fragment_ids",
    "fragments",
    "new_indices",
    "removed_indices",
    "groups",
    "version",
)

#: Operation fields whose own name collides with a row key, or reads wrong out of context.
#:
#: ``Restore.version`` is the dangerous one and the reason this map exists. Restore mints a *fresh* version
#: whose content equals an older one (``api/v1/endpoints/tables.py`` — restore commits rather than rewinds),
#: so the log shows v12 with ``operation: "Restore"`` and, without this field, no clue what it restored. But
#: copying it through under its own name would overwrite ``row["version"]`` — the log's primary key and the
#: join key for the lineage actor — turning "v12 restored from v7" into a row claiming to *be* v7. The most
#: valuable missing field and a silent corruption share one name, so it is renamed on the way out.
_TXN_FIELD_RENAMES = {"version": "restored_from"}


def table_history(ns: LanceNamespace, so: StorageOptions, table_id: list[str], limit: int = 50) -> list[dict[str, Any]]:
    """The table's commit log: one row per version, newest first — what changed, and when.

    Lance is immutable and append-only at the manifest level, so the history is not something we have to
    keep a side-table for: ``versions()`` supplies the WHEN and the transaction log supplies the WHAT
    (operation kind, the delete predicate as the caller wrote it, which fields an update rewrote, fragment
    deltas). This reads both and joins them by version.

    It deliberately does NOT answer WHO. Lance's transaction log has no notion of a user and should not —
    identity is our concern, not the format's. The actor per version lives in the lineage store's
    ``author`` run facet (``GET /datasets/{name}/producers`` returns ``dataset_version`` + ``author`` +
    ``operation``), so a caller that wants who/when/what joins this with that on the version number. Two
    sources, each authoritative for its own half; inventing a third would mean keeping a copy of one of
    them in sync.

    ``limit`` bounds the transaction reads, not the versions list: a table with 10k versions should not
    become 10k object-store round trips because a UI asked for a page.
    """
    dataset = open_dataset_unchecked(ns, so, table_id)
    versions = sorted(dataset.versions(), key=lambda v: int(v["version"]), reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for entry in versions:
        version = int(entry["version"])
        row: dict[str, Any] = {
            "version": version,
            "timestamp": committed_at(entry).isoformat(),
            "operation": None,
        }
        try:
            txn = dataset.read_transaction(version)
        except Exception as exc:
            # Versions written before transaction files, or a GC'd txn, still belong in the log. Reporting
            # the version with a null operation is honest; dropping it would make the history lie by omission.
            log.info("txn_unreadable", extra={"table": table_id, "version": version, "error": str(exc)})
            out.append(row)
            continue
        op = getattr(txn, "operation", None)
        # An operation this binding models no subclass for reaches here as the ABC itself, so
        # `type(op).__name__` would answer `BaseOperation` — the name of an abstract base class, not of
        # anything Lance recorded. One `compact_files()` commits such a version before its `Rewrite`
        # (measured 2026-09-11), so every compacted table would carry a row naming an operation no Lance
        # release defines. Null is the same answer the unreadable-transaction branch above already gives
        # to the same question, and needs no vocabulary a client must learn.
        if op is None or type(op) is lance.LanceOperation.BaseOperation:
            out.append(row)
            continue
        row["operation"] = type(op).__name__
        for field in _TXN_DETAIL_FIELDS:
            if not hasattr(op, field):
                continue
            value = getattr(op, field)
            # Fragment and index lists are long and carry no meaning in a log — the COUNT is the signal.
            row[_TXN_FIELD_RENAMES.get(field, field)] = len(value) if isinstance(value, (list, tuple)) else value
        # Did this version change the columns? Both attribute names must be tested: only ``Overwrite`` (a
        # create) carries ``new_schema``, while ``drop_columns`` commits a ``Project`` and ``add_columns``
        # commits a ``Merge`` — and both of those carry plain ``schema``. Testing only ``new_schema``
        # reported ``schema_set: false`` for a column drop, which is the log denying the one kind of change
        # a reader is most likely to be hunting for.
        row["schema_set"] = any(getattr(op, name, None) is not None for name in ("new_schema", "schema"))
        out.append(row)
    return out


#: Substrings that PROVE a ref operation failed on the VERSION rather than on the ref. Two spellings
#: because two layers answer: pylance's ref API raises `ValueError("Version not found error: version
#: main:999999 does not exist")`, while `create_branch` reaches the object store first and raises
#: `OSError("Dataset at path .../_versions/999999.manifest was not found")`. Measured on pylance 10.0.0.
_REF_VERSION_MISSING_MARKERS = ("version not found", ".manifest was not found")

#: The ref failure's own noun. pylance flattens every tag and branch failure into a bare
#: `ValueError` whose only discriminator is its text — the same constraint `_classify_commit_error`
#: works under, because the Rust layer's typed variants do not cross the pyo3 boundary.
#:
#: It captures the NOUN rather than testing `"branch" in message`, and that precision is load-bearing:
#: a tag literally named `branch` would make the substring test report code 22 for a missing tag. The
#: noun the message names also BEATS the caller's own kind, because a tag op scoped to a branch fails
#: on the BRANCH — answering 8 there sends the caller hunting a tag that was never the problem.
_REF_FAILURE_RE = re.compile(r"ref (?P<failure>not found|conflict) error:\s*(?P<noun>tag|branch)\b", re.IGNORECASE)

#: The ref-NAME validator's variant, raised before anything is looked up — a malformed name is a
#: malformed parameter, so it is `InvalidInput` (13 -> 400), never a not-found. Measured on pylance
#: 10.0.0 for both refs and every rule it enforces: invalid characters, a `.lock` suffix, a leading or
#: trailing `/`, consecutive `/`, and `..` inside a segment. The wording differs per ref ("Branch
#: segment ... contains invalid characters" vs "Ref characters must be either alphanumeric ..."), so
#: the shared PREFIX is what is matched.
_REF_INVALID_MARKER = "ref is invalid:"


#: The default ref. It is not in `branches.list()` on any dataset — the default is implicit — so the
#: create door's collision pre-check cannot see it, and pylance answers a request to create it with its
#: own bug-report text (measured on 11.0.0, 2026-09-15).
MAIN_BRANCH: Final = "main"


def recorded_branch(branch: object) -> str | None:
    """A ref as Lance records it: ``None`` for main, the branch name otherwise.

    Lance records main as null — a tag's ``branch`` and a branch's ``parentBranch``
    (``lance_docs/file_format.md`` "Tag File Format" / "Branch Metadata File Format") — and it stores a
    reference spelled ``("main", n)`` as null too (measured on pylance 12.0.0), while a request may name
    main by name. Comparing refs through this makes both spellings one ref.
    """
    if branch is None or branch == MAIN_BRANCH:
        return None
    if not isinstance(branch, str):
        raise TypeError(f"a branch is named by a str or None, got {type(branch).__name__}")
    return branch


def refuse_a_branch_name_the_backend_cannot_use(name: str) -> None:
    """Refuse the three branch names whose backend failure is not readable, with the spec's own codes.

    [[LH-046]] LANCE OWNS THE GRAMMAR AND THIS IS NOT A SECOND COPY OF IT. Most bad names — a space,
    `~ ^ : ? * [ ]`, `@`, a trailing `/`, a `.lock` suffix, a literal backslash — arrive as
    ``Ref is invalid: …`` and `_classify_ref_error` already maps that marker to InvalidInput. Widening
    this guard to duplicate that rule would create two grammars that drift, and the copy here is the one
    nobody re-tests against a new pylance. Driven on 11.0.0: `work`, `feature/x`, `dot.name`, `UPPER`
    and `-lead` are all ACCEPTED, so a hand-written pattern would have refused names that work.

    THREE CASES CARRY NO READABLE MARKER, and they are the ordinary ones:

    * ``main`` — absent from `branches.list()`, so the collision pre-check misses it and pylance answers
      ``Encountered internal error. Please file a bug report``. Answered as the spec's 23 rather than 13,
      because main does always exist; "already exists" is the true statement.
    * ``""`` — the spec marks `name` required but sets no `minLength`, so an empty string passes model
      validation and reaches pylance, which answers the same bug-report text.
    * a traversal segment — refused by the object-store PATH parser before Lance's ref validation runs,
      so the message is ``LanceError(IO): Error parsing Path …`` with no marker.

    Refused HERE rather than by matching those messages: the first two are pylance's bug-report text,
    which is not a stable discriminator, and the day upstream fixes that panic a matcher keyed on it
    would silently report Internal again. That is the same reasoning `create_branch` already gives for
    establishing a collision by reading the branch list instead of matching a message.
    """
    if name == MAIN_BRANCH:
        raise TableBranchAlreadyExistsError(f"branch {name!r} always exists and cannot be created")
    if not name.strip():
        raise InvalidInputError("branch name must not be empty")
    if ".." in name.split("/"):
        raise InvalidInputError(f"invalid branch name {name!r}: a path-traversal segment is not a branch name")


def refuse_a_branch_name_that_shadows_the_layout(name: str) -> None:
    """Refuse a branch name with a segment Lance uses as a directory name ([[LH-203]]).

    Lance's grammar accepts ``_`` in a segment, so ``a/_versions`` is a valid branch name, and the format
    joins it verbatim onto ``tree/``: its files land in ``tree/a/_versions/``, which is branch ``a``'s own
    version directory. That makes it the estate's rule rather than Lance's, so it is refused here as the
    caller's input (13), whether or not a branch ``a`` exists yet: once one does, the two share files.
    """
    if (segment := branch_layout.reserved_segment(name)) is not None:
        raise InvalidInputError(
            f"invalid branch name {name!r}: the segment {segment!r} is a directory name in a Lance dataset's layout, so the branch's files would land "
            "inside another ref's"
        )


def refuse_a_branch_name_that_nests_with_another(name: str, branches: Iterable[str]) -> None:
    """Refuse a name whose directory would lie inside an existing branch's, or contain one (409, code 19).

    `a/b` lives at ``tree/a/b/``, inside ``tree/a/``. Lance then treats ``a/b``'s files as part of ``a``'s
    directory: a delete of ``a`` leaves its files behind, and a vend or reclaim for ``a`` reaches ``a/b``.
    The name is well formed, so this is the table's state refusing it, which is the spec's 19
    InvalidTableState rather than 13.
    """
    existing = list(branches)
    if clash := branch_layout.branches_enclosing(name, existing) or branch_layout.branches_inside(name, existing):
        raise InvalidTableStateError(
            f"branch {name!r} cannot be created: its directory would nest with branch {clash[0]!r}'s, and a branch's directory holds only its own "
            "files. Choose a name that is not a '/'-prefix of an existing branch and has none"
        )


def _classify_ref_error(exc: Exception, *, kind: str, name: str, invalid_name: str | None = None) -> Exception:
    """Map a pylance tag/branch failure onto the Lance Namespace spec's coded error.

    Returns ``exc`` UNCHANGED when nothing matches, so an unrecognised failure stays an honest
    Internal rather than being forced into a code that would misdirect the caller.
    """
    message = str(exc)
    if _REF_INVALID_MARKER in message.lower():
        # A branch create names its SOURCE for a missing version but the NEW name for a malformed one.
        subject = name if invalid_name is None else invalid_name
        return InvalidInputError(f"invalid {kind} name {subject!r}: {exc}")
    if any(marker in message.lower() for marker in _REF_VERSION_MISSING_MARKERS):
        return TableVersionNotFoundError(f"no such version for {kind} {name!r}: {exc}")
    match = _REF_FAILURE_RE.search(message)
    if match is None:
        return exc
    noun = match.group("noun").lower()
    if match.group("failure").lower() == "conflict":
        if noun == "branch":
            return TableBranchAlreadyExistsError(f"branch {name!r} already exists")
        return TableTagAlreadyExistsError(f"tag {name!r} already exists")
    if noun == "branch":
        return TableBranchNotFoundError(f"branch {name!r} not found")
    return TableTagNotFoundError(f"tag {name!r} not found")


@contextmanager
def _ref_errors(kind: str, name: str, *, invalid_name: str | None = None) -> Iterator[None]:
    """Translate the tag/branch failures inside the block into their spec-coded errors.

    Without this every one of them reaches `install_problem_handlers` as a bare exception and is
    reported as **Internal 18** — a generated client dispatches on `code`, so "your tag already
    exists" arrives as "the server broke": unretryable, unactionable, and indistinguishable from a
    real fault in an alerting pipeline.
    """
    try:
        yield
    except (ValueError, OSError) as exc:
        translated = _classify_ref_error(exc, kind=kind, name=name, invalid_name=invalid_name)
        if translated is exc:
            raise
        raise translated from exc


def list_tags(ns: LanceNamespace, so: StorageOptions, req: ListTableTagsRequest) -> ListTableTagsResponse:
    """List the table's tags as ``{name: TagContents{version, manifest_size, branch}}``."""
    table_id = _table_id(req)
    tags: dict[str, dict[str, Any]] = {}
    for name, tag in open_dataset_unchecked(ns, so, table_id).tags.list().items():
        # pylance's Tag is a TypedDict (plain dict at runtime), so read by key.
        entry = tag if isinstance(tag, dict) else {"version": getattr(tag, "version", None)}
        tags[name] = {
            "version": entry.get("version"),
            "manifest_size": entry.get("manifest_size") or 0,
            "branch": entry.get("branch"),  # None for a tag on main (TagContents.branch is optional)
        }
    # model_validate coerces the inner dicts into TagContents (not exported to name directly).
    return ListTableTagsResponse.model_validate({"tags": tags})


def _tag_reference(branch: str | None, version: int | None) -> int | tuple[str | None, int | None] | None:
    """Map the spec's optional ``branch`` + ``version`` to a pylance tag ``reference``: a bare int resolves
    against the CURRENT branch (main), so a branch-scoped tag must pass the ``(branch, version)`` tuple."""
    return (branch, version) if branch is not None else version


def create_tag(ns: LanceNamespace, so: StorageOptions, req: CreateTableTagRequest) -> CreateTableTagResponse:
    """Tag the given table version with a name (honoring the request's optional ``branch``)."""
    with _ref_errors("tag", req.tag):
        open_dataset(ns, so, _table_id(req)).tags.create(req.tag, _tag_reference(req.branch, req.version))
    return CreateTableTagResponse()


def get_tag_version(ns: LanceNamespace, so: StorageOptions, req: GetTableTagVersionRequest) -> GetTableTagVersionResponse:
    """Return the table version a tag points to (404 on an unknown tag)."""
    tags = open_dataset_unchecked(ns, so, _table_id(req)).tags
    try:
        version = tags.get_version(req.tag)
    except ValueError as exc:  # pylance 8 raises ("Ref not found"), it does NOT return None
        raise TableTagNotFoundError(f"tag {req.tag!r} not found") from exc
    if version is None:  # kept for a future pylance that returns None instead
        raise TableTagNotFoundError(f"tag {req.tag!r} not found")
    # Echo the branch the tag lives on (None for main) so a non-main tag isn't reported as main.
    entry = tags.list().get(req.tag) or {}
    return GetTableTagVersionResponse(version=version, branch=entry.get("branch"))


def update_tag(ns: LanceNamespace, so: StorageOptions, req: UpdateTableTagRequest) -> UpdateTableTagResponse:
    """Move an existing tag to a new table version (honoring the request's optional ``branch``)."""
    with _ref_errors("tag", req.tag):
        open_dataset(ns, so, _table_id(req)).tags.update(req.tag, _tag_reference(req.branch, req.version))
    return UpdateTableTagResponse()


def delete_tag(ns: LanceNamespace, so: StorageOptions, req: DeleteTableTagRequest) -> DeleteTableTagResponse:
    """Delete a tag from the table."""
    with _ref_errors("tag", req.tag):
        open_dataset_unchecked(ns, so, _table_id(req)).tags.delete(req.tag)
    return DeleteTableTagResponse()


def _branch_reference(req: CreateTableBranchRequest) -> int | tuple[str | None, int | None] | None:
    """Map the spec's ``from_branch`` / ``from_version`` to pylance ``create_branch``'s ``reference``.

    fromBranch + fromVersion → ``(branch, version)``; fromBranch only → ``(branch, None)`` (latest of that
    branch); fromVersion only → the ``version`` int (on main); neither → ``None`` (latest of main).
    """
    if req.from_branch is not None:
        return (req.from_branch, req.from_version)
    if req.from_version is not None:
        return req.from_version
    return None


def list_branches(ns: LanceNamespace, so: StorageOptions, req: ListTableBranchesRequest) -> ListTableBranchesResponse:
    """List the table's branches as ``{name: BranchContents}``, read from pylance ``ds.branches``.

    The native ``DirectoryNamespace`` 501s branch ops, but ``lance.LanceDataset`` implements them, so we
    back them in-process here exactly like tags. A ``Branch`` is a TypedDict (plain dict at runtime).
    """
    branches: dict[str, dict[str, Any]] = {}
    for name, branch in open_dataset_unchecked(ns, so, _table_id(req)).branches.list().items():
        entry = branch if isinstance(branch, dict) else {}
        metadata = dict(entry.get("metadata") or {})
        # WHICH INCARNATION, in the one free-form map `BranchContents` has: the spec declares no field for
        # it, and a recreated branch restarts its numbering, so a reader holding `(branch, N)` from a
        # lineage event needs this to tell whether that N is still the same commit. Written over a user
        # key of the same name, because the value is Lance's record and not the user's.
        if (identifier := identifier_of(entry)) is not None:
            metadata[BRANCH_IDENTIFIER_KEY] = identifier
        branches[name] = {
            "parent_branch": entry.get("parent_branch"),
            "parent_version": entry.get("parent_version"),
            "create_at": entry.get("create_at"),
            "manifest_size": entry.get("manifest_size") or 0,
            "metadata": metadata,
        }
    return ListTableBranchesResponse.model_validate({"branches": branches})


def create_branch(ns: LanceNamespace, so: StorageOptions, req: CreateTableBranchRequest) -> CreateTableBranchResponse:
    """Create a branch from main (or a source branch/version) — maps to pylance ``create_branch``.

    A COLLISION IS ESTABLISHED BY READING, NOT BY MATCHING A MESSAGE, and this is the one ref failure
    where that distinction is forced. pylance answers an existing branch with its own bug-report text
    — ``OSError("Encountered internal error. Please file a bug report ... Clone operation should not
    enter build_manifest.")`` — which is neither a stable discriminator nor an honest thing to pattern
    match: the day upstream fixes that panic, a matcher keyed on it silently reports Internal again.
    So the door reads the branch list, and confirms by RE-READING when the create still fails, which
    also answers the create/create race the pre-check alone would lose.
    """
    # Names the backend cannot use are refused BEFORE the listing read: `main` is absent from
    # `branches.list()` (the default ref is implicit), so the collision check below cannot see it.
    refuse_a_branch_name_the_backend_cannot_use(req.name)
    refuse_a_branch_name_that_shadows_the_layout(req.name)
    table_id = _table_id(req)
    dataset = open_dataset(ns, so, table_id)
    branches = dataset.branches.list()
    if req.name in branches:
        raise TableBranchAlreadyExistsError(f"branch {req.name!r} already exists")
    refuse_a_branch_name_that_nests_with_another(req.name, branches)
    # THE SOURCE IS ESTABLISHED BY READING TOO, and here the message leaves no choice: pylance renders
    # a missing source BRANCH and a missing source VERSION with the same object-store text, so a
    # classifier reading it alone answers 11 for a branch that does not exist — the wrong code, and a
    # caller sent hunting a version when the branch is what is absent. With no `from_version` at all it
    # renders differently again and matches nothing, falling through as Internal 18.
    if req.from_branch is not None and req.from_branch not in branches:
        raise TableBranchNotFoundError(f"source branch {req.from_branch!r} not found")
    # The residual version failure belongs to the SOURCE, so name that — not the branch being created,
    # which exists nowhere yet and tells the caller nothing about what was missing.
    source = req.from_branch if req.from_branch is not None else MAIN_BRANCH
    try:
        with _ref_errors("branch", source, invalid_name=req.name):
            dataset.create_branch(req.name, _branch_reference(req))
    except OSError:
        if req.name in open_dataset(ns, so, table_id).branches.list():
            raise TableBranchAlreadyExistsError(f"branch {req.name!r} already exists") from None
        raise
    return CreateTableBranchResponse()


def delete_branch(ns: LanceNamespace, so: StorageOptions, req: DeleteTableBranchRequest) -> DeleteTableBranchResponse:
    """Delete a branch from the table, and every branch whose directory lies inside its own ([[LH-203]]).

    THROUGH THE NESTED BRANCHES FIRST. Lance keeps a directory another branch lives in, so deleting
    ``a`` while ``a/b`` exists removed only ``a``'s ref and left ``tree/a``'s manifests, transactions and
    data behind for nothing to reclaim (measured on pylance 12.0.0). Deleting deepest first and ``a``
    last lets each delete remove its whole directory. The create door refuses a name that nests, so
    only a table that predates that refusal can carry such a pair.

    A branch that lies inside ANOTHER branch's files is refused instead (409): Lance removes the
    branch's directory recursively, so deleting ``a/_versions`` deletes ``tree/a/_versions``, which is
    ``a``'s whole history, and ``a`` stops opening. Deleting ``a`` removes both.
    """
    dataset = open_dataset_unchecked(ns, so, _table_id(req))
    listed = dataset.branches.list()
    branches = list(listed)
    # Read before anything is deleted: a missing `a` with an `a/b` would otherwise delete `a/b` and then fail.
    if req.name not in branches:
        raise TableBranchNotFoundError(f"branch {req.name!r} not found")
    holders = [parent for parent in branch_layout.branches_enclosing(req.name, branches) if branch_layout.lies_in_files_of(req.name, parent)]
    if holders:
        raise InvalidTableStateError(
            f"branch {req.name!r} lies inside the files of branch {holders[0]!r}: deleting it would delete {holders[0]!r}'s files too. "
            f"Delete {holders[0]!r}, which deletes {req.name!r} with it. Nothing was deleted"
        )
    doomed = [*branch_layout.branches_inside(req.name, branches), req.name]
    # A BRANCH CUT FROM ONE OF THEM pins it: pylance refuses to delete a branch another is forked from
    # ("Ref conflict error: Branch a is referenced by [("c", 1)]", measured on 12.0.0). Asked before the
    # first delete, so the cascade never removes `a/b` and then stops at `a`.
    forks = sorted(child for child, meta in listed.items() if child not in doomed and (meta or {}).get("parent_branch") in doomed)
    if forks:
        raise _forked_from(req.name, forks)
    deleted: list[str] = []
    for name in doomed:
        try:
            with _ref_errors("branch", name):
                dataset.branches.delete(name)
        except TableBranchAlreadyExistsError as exc:
            # The ref-conflict marker here is a fork pin made after the read above, not a name collision.
            raise InvalidTableStateError(
                f"deleting branch {req.name!r} stopped at {name!r}: a branch forked from it since the check pins it ({exc.__cause__}). "
                f"Deleted before stopping: {deleted}"
            ) from exc
        deleted.append(name)
    return DeleteTableBranchResponse()


def _forked_from(name: str, forks: Sequence[str]) -> InvalidTableStateError:
    return InvalidTableStateError(
        f"branch {name!r} cannot be deleted: {', '.join(repr(f) for f in forks)} is forked from it or from a branch inside it, and a fork "
        "pins its parent. Delete the fork first. Nothing was deleted"
    )


def ensure_merge_key_index(ns: LanceNamespace, segments: list[str], on: str | None, *, so: StorageOptions | None = None, branch: str | None = None) -> None:
    """Best-effort BTREE index on a merge key, built AFTER the first ``/merge_insert`` commits (§4).

    pylance's ``use_index=True`` default only helps *"if an index is available"*, and no automatic
    data-flow ever builds one — so without this, every upsert full-scans the ``on`` column and merge
    latency decays as the table grows (the namespace spec's own ``__manifest`` design mandates exactly
    this pairing: merge-insert PK dedup WITH a BTREE on the key).

    LIST FIRST is required, not an optimization: pylance's ``create_scalar_index`` defaults
    ``replace=True``, so an unconditional build would full-scan and REBUILD the column on every upsert
    — turning the fix into a regression.

    A BRANCH BUILDS THROUGH THE DATASET HANDLE, and that is the answer to the note that stood here.
    It said the dir backend's handling of ``branch`` on an index build was unverified at pylance 8.0.0
    and the param was forwarded either way. The live pass it asked for happened 2026-08-31 and the
    answer is NO: the native op accepts ``branch`` and builds on MAIN, so a branch-scoped
    ``/merge_insert`` merged correctly into the branch and then committed an index version to main —
    the write isolated, its accelerator not. Measured against pylance 8.0.0: the same build through
    ``checkout_version((branch, None))`` leaves main at its version and puts the index on the branch.
    Main keeps the native op, which is already correct.

    Best-effort by contract: an index-build failure or a CreateIndex commit conflict must never fail
    the write that already committed — any failure logs and returns.
    """
    if not on:
        return
    try:
        if branch is not None and so is not None:
            dataset = open_dataset(ns, so, segments, branch=branch)
            if any(on in (index.get("fields") or []) for index in dataset.list_indices()):
                return
            dataset.create_scalar_index(on, index_type="BTREE")
            log.info("merge_key_index_built", extra={"table": "/".join(segments), "column": on, "branch": branch})
            return
        listing = native.call(ns, "list_table_indices", ListTableIndicesRequest(id=segments, branch=branch))
        for index in listing.indexes or []:
            if on in (index.columns or []):
                # An existing index of ANY type covering the key skips the build (never rebuild —
                # replace=True!). Accepted per the §4 spec wording; a non-BTREE index on the key
                # (BITMAP/INVERTED) therefore also suppresses the BTREE — revisit only if merge dedup
                # proves unable to use those.
                return
        native.call(
            ns,
            "create_table_scalar_index",
            CreateTableIndexRequest(id=segments, column=on, index_type="BTREE", branch=branch),
        )
        log.info("merge_key_index_built", extra={"table": "/".join(segments), "column": on})
    except UnsupportedOperationError as exc:
        # A backend that 501s the list/build ops will NEVER get the accelerator — distinct event so
        # operators can tell "permanently unsupported here" from a transient failure below.
        log.warning(
            "merge_key_index_unsupported",
            extra={"table": "/".join(segments), "column": on, "error": str(exc)},
        )
    except Exception as exc:
        log.warning(
            "merge_key_index_skipped",
            extra={"table": "/".join(segments), "column": on, "error": str(exc)},
        )
