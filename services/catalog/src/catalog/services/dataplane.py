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
import logging
import re
import uuid
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import replace
from typing import Any, Final, cast

import lance
import pyarrow as pa
import pyarrow.fs as pafs
from botocore.exceptions import BotoCoreError, ClientError
from lance.dataset import Transaction
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
    CreateTableResponse,
    CreateTableTagRequest,
    CreateTableTagResponse,
    CreateTableVersionRequest,
    DeleteFromTableRequest,
    DeleteFromTableResponse,
    DeleteTableBranchRequest,
    DeleteTableBranchResponse,
    DeleteTableTagRequest,
    DeleteTableTagResponse,
    DeregisterTableRequest,
    DeregisterTableResponse,
    DescribeTableRequest,
    DescribeTransactionRequest,
    DropTableRequest,
    DropTableResponse,
    GetTableTagVersionRequest,
    GetTableTagVersionResponse,
    InsertIntoTableRequest,
    InsertIntoTableResponse,
    InternalError,
    InvalidInputError,
    InvalidTableStateError,
    LanceNamespace,
    LanceNamespaceError,
    ListTableBranchesRequest,
    ListTableBranchesResponse,
    ListTableTagsRequest,
    ListTableTagsResponse,
    MergeInsertIntoTableRequest,
    MergeInsertIntoTableResponse,
    NamespaceExistsRequest,
    RegisterTableRequest,
    RestoreTableRequest,
    RestoreTableResponse,
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

from catalog.api.pagination import paginate
from catalog.core import manifest_bytes, provenance_guard
from catalog.core.config import shared_lance_session
from catalog.core.identifiers import rename_destination
from catalog.core.modes import CreateMode, InsertMode
from catalog.core.namespace import disarm_commit_path_cleanup, open_resolved_dataset, open_resolved_dataset_unchecked
from catalog.services import changes, native, primary_keys, table_bases, table_claims
from catalog.services.cast_size import bytes_after_cast
from service_kit.lakehouse import auto_cleanup, base_registry, branch_layout, data_directories, location_claims, trash
from service_kit.lakehouse.base_refs import decoded_path, decoded_segments, location_in_store, normalise, store_of
from service_kit.lakehouse.control_prefixes import CONTROL_PREFIXES
from service_kit.lakehouse.features import manifest_base_path_refs
from service_kit.lakehouse.history_fences import HistoryWork
from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lakehouse.ns_errors import PartiallyApplied
from service_kit.lakehouse.objectfs import StorageOptions, credential_of, s3_filesystem
from service_kit.lakehouse.schema import SchemaFields, facet_fields
from service_kit.lakehouse.stage_stamp import declare_dataset_id
from service_kit.lakehouse.version_retention import MAIN_BRANCH, recorded_branch
from service_kit.lancekit.arrow_ipc import ArrowBodyError, ArrowBodyTooLargeError, decode_arrow_stream, encode_arrow_stream
from service_kit.lancekit.commit_verdict import CommitVerdict, classify_commit_failure
from service_kit.lancekit.outage import reads_as_store_outage
from service_kit.lancekit.versions import committed_at
from storage import s3_client, split_s3_uri


log = logging.getLogger(__name__)


def read_version_and_schema(
    location: str,
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

    ``location`` is where the write committed, the location its door resolved ([[LH-530]]): a name read again after
    the commit may name another table by then. ``branch`` must name the ref the write COMMITTED TO: a branch keeps its
    own version sequence, so version N opened on main is a different snapshot.

    Entirely best-effort: the write is already committed, so a readback failure must degrade the lineage
    enrichment (``(pin_version, [], None)``), never fail the request.
    """
    try:
        dataset = open_resolved_dataset(location, so, table_id, version=pin_version, branch=branch)
        # The physical URI rides the SAME handle, so the standard `dataSource` facet costs nothing
        # beyond this open. Without it #23 reconcile has no URI to read and reports every live table
        # `missing_on_storage`, and an event-driven consumer receives a table id it cannot resolve —
        # `services/maintenance` holds no catalog client by design.
        uri = str(getattr(dataset, "uri", "") or "") or None
    except Exception as exc:
        log.warning("lineage_readback_failed", extra={"table": table_id, "error": str(exc)})
        return pin_version, [], None
    if pin_version is None:
        return None, [], uri
    try:
        return pin_version, facet_fields(dataset.schema), uri
    except Exception as exc:
        log.warning("schema_facet_read_failed", extra={"table": table_id, "error": str(exc)})
        return pin_version, [], uri


#: How far below a branch's head :func:`committed_version` looks for a transaction. The commit being
#: identified has just landed, so it sits at the head unless other writers committed after it; the bound
#: keeps a lookup on a busy branch from walking its whole history.
_TRANSACTION_SEARCH_DEPTH: Final = 64


def committed_version(table: table_claims.DoorTable, so: StorageOptions, transaction_id: str | None, *, branch: str | None) -> int | None:
    """The version the transaction ``transaction_id`` committed on ``branch`` of the door's ``table``, or ``None`` when it cannot be found.

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

    Main's lookup names the table, which only that table's own history answers, since the transaction id is the
    commit's; a branch's opens the location the door resolved. Read after the write, so neither asks whether the
    name still resolves there.

    Best-effort: ``None`` makes the event versionless, never the request a failure.
    """
    if not transaction_id:
        return None
    table_id = table.segments
    try:
        if recorded_branch(branch) is None:
            described = table.namespace.describe_transaction(DescribeTransactionRequest(id=[*table_id, transaction_id]))
            raw = (described.properties or {}).get("version")
            return int(raw) if raw is not None else None
        dataset = open_resolved_dataset_unchecked(table.location, so, table_id, branch=branch)
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
        # `BaseException`, because a pyo3 panic derives from
        # BaseException and cannot be caught by name (`pyo3_runtime` is synthesised lazily and is not
        # importable). This runs after the write committed, so an escaped panic answers 500 for a commit
        # that happened and skips the door's idempotency record, and a retry commits again.
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        log.warning("committed_version_unreadable", extra={"table": table_id, "branch": branch, "error": str(exc), "error_type": type(exc).__name__})
    return None


def branch_identifier(location: str, so: StorageOptions, table_id: list[str], branch: str | None) -> str | None:
    """The identifier of the CURRENT incarnation of ``branch`` of the table at ``location``, or ``None`` for main or when unreadable.

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
        entry = open_resolved_dataset_unchecked(location, so, table_id).branches.list().get(name)
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
    `docs/adr/0066-four-rulings-on-the-lakehouse-s-unblocked-rows-owner-2026.md`. General compression runs AFTER FSST/bitpacking/RLE, so on small values it adds a
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


class DataBase(BaseModel):
    """A data base as ONE table registers it: that table's own directory under an approved base ([[LH-252]])."""

    #: ``<approved base>/<table directory>`` — the path the manifest records and every read resolves through.
    path: str
    #: The name ``target_bases`` routes fragments by, derived from the APPROVED base so a re-sent
    #: ``data_base`` names the same registered base on every write.
    name: str


def table_data_bases(approved: Sequence[str], directory: str) -> list[DataBase]:
    """The data bases a CREATE registers: one directory per approved base, all named ``directory``.

    A TABLE'S OWN DIRECTORY, never the approved base itself. Registered at the base, every table naming
    it round-robined its fragments into one flat ``<base>/``, and a vend for one table granted READ on
    ``<base>/*`` — every sibling's fragments. Under ``<base>/<directory>/`` a table's files are a prefix
    no other table writes, so the session policy can grant exactly that prefix. Measured on pylance
    12.0.0: ``DatasetBasePath(<base>/<dir>, is_dataset_root=False)`` writes its fragments under
    ``<dir>/`` and reads them back (``file_format.md`` § Base Path System: a non-root base "points
    directly to the file directory").
    """
    approved = list(dict.fromkeys(approved))
    names = [_base_name(uri) for uri in approved]
    # _base_name is lossy (s3://b/a/c and s3://b/a-c both → b-a-c). A collision would silently make one
    # approved base unaddressable + make target resolution ambiguous — reject it loudly, never misroute.
    if len(set(names)) != len(names):
        raise InvalidInputError(f"data_base paths collide on base name {names}; use distinct base paths")
    return [DataBase(path=f"{uri.rstrip('/')}/{directory}", name=name) for uri, name in zip(approved, names, strict=True)]


def registered_data_bases(approved: Sequence[str], dataset: lance.LanceDataset) -> list[DataBase]:
    """The data bases an OVERWRITE targets: each approved base's directory as the table's manifest registered it.

    Bases register at create only, so the names a re-sent ``data_base`` derives must match a base the
    manifest already holds; an approved base this table never registered is refused here rather than
    reaching pylance as an unknown target name.
    """
    registered = {ref.name: ref.path for ref in manifest_base_path_refs(dataset) if ref.name}
    bases: list[DataBase] = []
    for uri in dict.fromkeys(approved):
        name = _base_name(uri)
        if name not in registered:
            raise InvalidInputError(f"data_base {uri!r} is not registered on this table; a table's data bases register when it is created")
        bases.append(DataBase(path=registered[name], name=name))
    return bases


def _write_blob(
    table: pa.Table,
    uri: str,
    so: StorageOptions,
    *,
    mode: str,
    allow_external: bool,
    external_blob_bases: list[str],
    data_bases: list[DataBase] | None = None,
    properties: dict[str, str] | None = None,
    transaction_properties: dict[str, str] | None = None,
) -> lance.LanceDataset:
    """Write a table at file format 2.2 with stable row ids.

    ``allow_external`` opts into ``Blob.from_uri`` columns ANYWHERE outside the dataset root (blanket bypass);
    ``external_blob_bases`` registers approved base URIs so external pointers UNDER a registered base are
    accepted with the bypass left off — the safer allowlist posture (lance_docs/guide.md). ``data_bases``
    (#3-B) are the table's own directories under approved DATA-distribution bases, which the fragments
    round-robin across (the Uber pattern): :func:`table_data_bases` on a create,
    :func:`registered_data_bases` on an overwrite, which reuses the bases the table registered at create."""
    is_create = mode == "create"
    table = _apply_encoding(table, properties)
    data_bases = data_bases or []
    # DatasetBasePath registers each base (is_dataset_root=False = a raw data location, not a nested
    # dataset). initial_bases REGISTERS the bases in the manifest — CREATE-only (an overwrite/append reuses
    # the already-registered set; re-registering on overwrite is rejected by pylance).
    external_paths = [lance.DatasetBasePath(b, is_dataset_root=False) for b in external_blob_bases]
    data_paths = [lance.DatasetBasePath(base.path, is_dataset_root=False, name=base.name) for base in data_bases]
    _has_bases = bool(external_blob_bases or data_bases)
    initial_bases = (external_paths + data_paths) if is_create and _has_bases else None
    # target_bases is the WRITE TARGET: it round-robins the FRAGMENT writes across the data bases while the
    # manifest + _versions stay in the primary root (relative-path portable — a relocation moves the base
    # URIs, not 10M file paths). Applied on ANY mode when data_bases is SUPPLIED, so a re-supplied overwrite
    # ALSO distributes. CAVEAT: a mutation that does NOT re-send data_base (a bare overwrite, or the
    # /insert append route which has no data_base param) concentrates its NEW fragments in the primary root —
    # create-time distribution is the firm guarantee; per-write distribution needs the bases re-supplied.
    target_bases = [base.name for base in data_bases] or None
    # Every base writes under the root's own options, Lance's default (`lance_docs/guide.md` § Per-Base
    # Configuration: "every base inherits the unscoped options"): a data base on the estate's store is the table's own
    # storage, reached under the catalog's identity as the root is ([[LH-286]]).
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
            allow_external_blob_outside_bases=allow_external,
            transaction_properties=transaction_properties,
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


#: The marker the `dir` catalog's DeclareTable writes at a table's location, so that a declared table's directory holds
#: an object before any data lands (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md:143`).
RESERVED_MARKER: Final = ".lance-reserved"


def declare_table(ns: LanceNamespace, so: StorageOptions, segments: list[str], *, root: str, claims: location_claims.ClaimStore | None, holder: str) -> str:
    """Declare ``segments`` at ``<root>/<table uuid>``, where a table the catalog makes lives, and answer that location ([[LH-520]]).

    The `dir` catalog's own DeclareTable steps (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md:132-149`)
    with the catalog choosing the location, as the spec leaves it to (`lance_docs/ns_catalog/spec.yaml:3823-3827`). The
    backend's DeclareTable puts the name in the path (`<hash>_<object_id>`) and refuses any other location (measured on
    pylance 13.0.0), so ADR 0173 §5 takes the steps here, in this order:

    1. the table's uuid is minted and its location's claim taken under it, before any backend call, so the location is
       this table's before anything resolves to it;
    2. the `.lance-reserved` marker is written at the location;
    3. the ``__manifest`` row is recorded through RegisterTable's relative location. Measured on pylance 13.0.0, such a
       row answers ``is_only_declared`` until data lands, as the backend's own declaration does, and keeps no properties.

    A failure after the claim removes the marker and releases the claim. ``claims`` is ``None`` for a caller outside
    governance (a test building a fixture), which takes no claim.

    Raises:
        InvalidInputError: ``segments`` names a table outside every namespace: the `dir` backend resolves a root-level
            table by its ``<name>.lance`` directory, never by its row (measured on pylance 13.0.0).
        ConcurrentModificationError: Another table holds the location.
        TableAlreadyExistsError: A table already holds the name.
    """
    if len(segments) < 2:
        raise InvalidInputError(f"table {segments!r} names no namespace; a table the catalog makes lives in one")
    table_uuid = str(uuid.uuid4())
    location = f"{root.rstrip('/')}/{table_uuid}"
    if claims is not None:
        try:
            table_claims.claim_new_table(ns, claims, location, table=holder, segments=segments, table_uuid=table_uuid)
        except location_claims.LocationHeldError:
            raise ConcurrentModificationError(f"the location minted for table {holder} is held by another table; retry to be given another") from None
    try:
        _reserve(location, so)
        native.call(ns, "register_table", RegisterTableRequest(id=segments, location=table_uuid))
    except Exception as exc:
        _unreserve(location, so, claims=claims, holder=holder)
        # The `dir` backend keeps namespaces and tables in one object-id space and arbitrates the add by
        # put-if-not-exists, so a namespace at the id surfaces as the add's conflict: a TableAlreadyExists on a local
        # root, a ConcurrentModification (14) on the e2e-stack lane's store (run 39029359505). Neither a retry nor a
        # table holds the id, so the answer is the already-exists code naming what does.
        if isinstance(exc, TableAlreadyExistsError | ConcurrentModificationError) and _namespace_holds(ns, segments):
            raise TableAlreadyExistsError(f"a namespace already holds the id {'$'.join(segments)}; a table cannot be created there") from exc
        raise
    return _described_location(ns, segments, fallback=location)


def _namespace_holds(ns: LanceNamespace, segments: list[str]) -> bool:
    """Whether a namespace, and no table, is at ``segments``; an unreadable answer is not one.

    The `dir` backend's ``namespace_exists`` answers present for a table's id as well (measured on pylance 13.0.0), so
    a table answering at the id means the holder is a table: a second declaration, or two creates racing.
    """
    try:
        ns.namespace_exists(NamespaceExistsRequest(id=segments))
    except Exception:  # noqa: BLE001 — only a namespace that answers present changes the conflict's wording
        return False
    return _existing_location(ns, segments) is None


def _reserve(location: str, so: StorageOptions) -> None:
    """Write the `.lance-reserved` marker at ``location``, as :func:`delete_table_object` deletes: no directory object beside it."""
    if location.startswith("s3://") and so.get("endpoint"):
        bucket, prefix = split_s3_uri(location.rstrip("/"))
        _object_client(so).put_object(Bucket=bucket, Key=f"{prefix}/{RESERVED_MARKER}", Body=b"")
        return
    fs, path = dataset_fs(location, so)
    fs.create_dir(path, recursive=True)
    with fs.open_output_stream(f"{path}/{RESERVED_MARKER}"):
        pass


def _unreserve(location: str, so: StorageOptions, *, claims: location_claims.ClaimStore | None, holder: str) -> None:
    """Undo a declaration that failed after its claim: the marker goes, then the claim. Logged, never raised over the failure."""
    try:
        delete_table_object(location, so, RESERVED_MARKER)
        if claims is not None:
            location_claims.release(claims, location, holder)
    except Exception as exc:  # noqa: BLE001 — the declaration's own failure is what the caller is told
        log.error("declared_location_not_released", extra={"table": holder, "location": location, "error": str(exc)[:300]})


def _described_location(ns: LanceNamespace, segments: list[str], *, fallback: str) -> str:
    """The location the backend describes ``segments`` at, spelled as every other door answers it; ``fallback`` when it does not answer."""
    try:
        described = native.call(ns, "describe_table", DescribeTableRequest(id=segments, check_declared=True))
    except Exception as exc:  # noqa: BLE001 — the declaration committed; the caller keeps the location it was made at
        log.warning("declared_location_unresolved", extra={"table": segments, "error": str(exc)[:300]})
        return fallback
    return str(described.location or fallback)


def create_table(
    ns: LanceNamespace,
    so: StorageOptions,
    segments: list[str],
    table: pa.Table,
    *,
    root: str,
    mode: str | CreateMode | None = None,
    properties: dict[str, str] | None = None,
    allow_external_blobs: bool = False,
    external_blob_bases: list[str] | None = None,
    data_bases: list[str] | None = None,
    registry: base_registry.BaseRegistry | None,
    holder: str = "",
    transaction_properties: dict[str, str] | None = None,
    overwriting: table_claims.DoorTable | None = None,
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
    A brand-new table is declared at ``<root>/<table uuid>`` (:func:`declare_table`, ADR 0173 §5), the namespace
    connection's ``root``, then written at ``data_storage_version="2.2"`` (lance_docs/guide.md — Version
    Compatibility). Any failed fresh write is rolled back with ``drop_table`` so the name stays retryable rather than
    stuck describable-but-unreadable.

    ``properties`` are stamped on the table's schema metadata in the data's commit ([[LH-503]]) and kept nowhere else:
    the declaration is a RegisterTable, which keeps none, as the spec allows (`lance_docs/ns_catalog/spec.yaml:2719-2724`),
    so the response names none either.

    ``registry`` is where a fresh write records the bases it registers ([[LH-279]]): the record is the
    catalog's word on which foreign bases the table may declare, written BEFORE the manifest and removed
    again when the write fails. Required and never defaulted — the governed door passes the control root;
    ``None`` is an explicit choice for a caller outside governance (a test building a fixture), which
    leaves the table with no record at all. The table's location claim, which carries its uuid ([[LH-519]],
    ADR 0173 §6), lives beside the record: a table this call declares is given the uuid its location is named
    after, and one an earlier request declared keeps the uuid its declaration's claim records. ``holder`` is the
    table's canonical id, required whenever ``registry`` is given.

    ``transaction_properties`` ride the commit that writes the data (the run a governed create names).

    ``overwriting`` is the table an ``Overwrite`` asked ``can_drop`` on, as its door resolved it ([[LH-530]]): the write
    lands at that location once the name still resolves there.

    Raises:
        ConcurrentModificationError: The table an ``Overwrite`` resolved changed since.
    """
    if registry is not None and not holder:
        raise ValueError("a governed create names the table that holds its location and data directories")
    allow_external = allow_external_blobs
    external_blob_bases = external_blob_bases or []
    normalized = CreateMode.parse(mode)
    existing = overwriting.location if overwriting is not None else _existing_location(ns, segments)
    only_declared = existing is not None and not holds_a_version(existing, so)

    if existing is not None and not only_declared:  # a written, readable table already lives here
        if normalized is CreateMode.OVERWRITE:
            # A new version of the same table, so the table's provenance rides onto it ([[LH-242]]).
            # Disarmed first ([[LH-245]]): an overwrite carries the table's config forward, keys included.
            opened = overwriting.open_unchecked(so) if overwriting is not None else lance.dataset(existing, storage_options=so, session=shared_lance_session())
            current = disarm_commit_path_cleanup(opened, segments)
            # The table's key, not the payload's: a key is fixed once set (`lance_docs/file_format.md:4684`).
            primary_keys.refuse_repeats_within(table, primary_keys.key_columns(current.lance_schema), door="create")
            dataset = _write_blob(
                provenance_guard.keep_provenance(current.schema, table),
                existing,
                so,
                mode="overwrite",
                allow_external=allow_external,
                external_blob_bases=external_blob_bases,
                data_bases=registered_data_bases(data_bases, current) if data_bases else None,
                properties=properties,
                transaction_properties=transaction_properties,
            )
            return CreateTableResponse(location=existing, version=dataset.version)
        if normalized is CreateMode.EXIST_OK:  # keep it untouched, just report its current version
            return _kept_at(existing, so)
        # A written table under `Create` is the create's conflict, whatever the payload holds: the spec's 409 comes
        # before a judgement of rows that will never be written ([[LH-424]]).
        raise TableAlreadyExistsError(f"Table already exists: {'$'.join(segments)}")

    # BEFORE the declare, so a refused create leaves no name behind ([[LH-243]]).
    primary_keys.refuse_repeats_within(table, primary_keys.declared_key(table.schema), door="create")
    if existing is not None and only_declared:  # declared, no data yet → write into it (all modes)
        if overwriting is not None:
            overwriting.require_held()
        if registry is not None:
            table_claims.take(ns, _claim_store(registry), existing, holder, segments)
        return _write_blob_into(
            ns,
            _named_by_its_uuid(table, existing, registry),
            existing,
            so,
            segments,
            properties,
            allow_external=allow_external,
            external_blob_bases=external_blob_bases,
            data_bases=data_bases,
            registry=registry,
            holder=holder,
            transaction_properties=transaction_properties,
        )

    location = declare_table(ns, so, segments, root=root, claims=None if registry is None else _claim_store(registry), holder=holder)
    return _write_blob_into(
        ns,
        _named_by_its_uuid(table, location, registry),
        location,
        so,
        segments,
        properties,
        allow_external=allow_external,
        external_blob_bases=external_blob_bases,
        data_bases=data_bases,
        registry=registry,
        holder=holder,
        transaction_properties=transaction_properties,
    )


def _named_by_its_uuid(table: pa.Table, location: str, registry: base_registry.BaseRegistry | None) -> pa.Table:
    """``table`` with ``lineage.dataset_id`` naming the uuid its location's claim records (ADR 0173 §10), for a table this
    create brings into being; an Overwrite keeps the table's own (:func:`provenance_guard.keep_provenance`).

    The uuid, not the name: a name can be given up and taken by another table, so a file stamped with one names whichever
    table holds it today. A governed create claims the location before writing, so the claim is there to read; ``None``
    for ``registry``, a caller outside governance, stamps nothing.
    """
    if registry is None:
        return table
    claim = location_claims.read(_claim_store(registry), location)
    return declare_dataset_id(table, claim.claim.uuid if claim is not None else None)


def _claim_store(registry: base_registry.BaseRegistry) -> location_claims.ClaimStore:
    """The location claims beside ``registry``'s base records, on the same control root and credential."""
    return location_claims.ClaimStore(control_root=registry.control_root, storage_options=registry.storage_options)


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
    registry: base_registry.BaseRegistry | None,
    holder: str = "",
    transaction_properties: dict[str, str] | None = None,
) -> CreateTableResponse:
    """Write the blob table's first data version into an already-declared ``location``, rolling the declare
    back with ``drop_table`` on failure so the name stays retryable rather than stuck declared-but-unreadable.

    THE BASE RECORD IS CLAIMED FIRST ([[LH-279]]): every base this write registers through
    ``initial_bases`` — the external blob base the create asked for and the approved data bases — is in the
    catalog's record before the manifest that declares them exists, so no reader ever meets the table
    declaring a base its record lacks. A failed write releases exactly what it claimed; a record that
    outlived its write would vouch for a base no manifest names.

    EACH DATA DIRECTORY IS CLAIMED FOR ``holder`` FIRST OF ALL ([[LH-252]]), put-if-not-exists, so no other
    table — a registration declaring the same directory — can be admitted to it while this one exists.

    The location's own claim is :func:`create_table`'s, taken before this write ([[LH-519]]); a failed write
    releases it with the table it drops.
    """
    claim: base_registry.BaseClaim | None = None
    directories: list[str] = []
    try:
        # ONE DIRECTORY PER TABLE under each approved base ([[LH-252]]), minted here because the record
        # below and the manifest must name the same path.
        own_bases = table_data_bases(data_bases or [], uuid.uuid4().hex)
        if registry is not None:
            entries = table_bases.create_entries(external_blob_bases, [(base.path, base.name) for base in own_bases])
            directories = base_registry.claim_data_directories(
                registry, entries, holder, segments, is_gone=lambda held: table_claims.data_directory_holder_is_gone(ns, _claim_store(registry), held)
            )
            claim = base_registry.claim_bases(registry, location, entries)
        # STAMPED ON THE TABLE, not only on the manifest row and the reply. The spec's "properties at create" was true of
        # `declare_table` and of the response echo and false of the Lance file, so a client that created with
        # `{"owner": …}` and then asked the TABLE got nothing back and no error saying why. Written as the table's schema
        # metadata IN THE DATA'S COMMIT, the one that carries the writer's run and author ([[LH-503]]): a commit of
        # their own after it would be a version naming neither. MERGED over the payload's own metadata, the property
        # winning, as `update_schema_metadata` merges.
        if properties:
            table = table.replace_schema_metadata({**(table.schema.metadata or {}), **{key.encode(): value.encode() for key, value in properties.items()}})
        dataset = _write_blob(
            table,
            location,
            so,
            mode="create",
            allow_external=allow_external,
            external_blob_bases=external_blob_bases,
            data_bases=own_bases,
            properties=properties,
            transaction_properties=transaction_properties,
        )
    except BaseException as exc:
        # A Rust PANIC is a pyo3 `PanicException`, which derives from BaseException, so `except Exception` would skip
        # the rollback and leave the name declared with no dataset behind it ([[LH-302]]).
        with suppress(Exception):  # best-effort rollback; re-raise the real write error
            ns.drop_table(DropTableRequest(id=segments))
        if registry is not None:
            try:
                if claim is not None:
                    base_registry.release_claim(registry, claim)
                base_registry.release_data_directories(registry, directories, holder)
                location_claims.release(_claim_store(registry), location, holder)
            except Exception as release_exc:  # noqa: BLE001 — the write error is what the caller is told
                log.error("create_base_record_release_failed", extra={"location": location, "error": str(release_exc)[:300]})
        if isinstance(exc, Exception | KeyboardInterrupt | SystemExit | GeneratorExit):
            if isinstance(exc, OSError) and _UNSUPPORTED_TYPE_MARKER in str(exc):
                raise InvalidInputError(f"the table's schema holds a type Lance does not store: {clean_lance_message(str(exc))}") from exc
            raise
        raise InternalError("the table's first write panicked in Lance; the create was rolled back and may be retried") from exc
    return CreateTableResponse(location=location, version=dataset.version)


#: Lance's refusal of an Arrow type it does not store, as a bare `OSError`: "LanceError(Schema): Unsupported data type:
#: ListView(...)" for list_view, large_list_view, run-end encoded, union, month_day_nano and decimal32 columns (measured
#: on pylance 13.0.0). The caller's schema, so InvalidInput (13) rather than Internal ([[LH-301]]).
_UNSUPPORTED_TYPE_MARKER = "Unsupported data type:"


def kept_table(ns: LanceNamespace, so: StorageOptions, segments: list[str]) -> CreateTableResponse | None:
    """What an ExistOk create keeps at ``segments``: the written table's location and version, or ``None`` while no
    table there holds a version (none at all, or one only declared)."""
    existing = _existing_location(ns, segments)
    if existing is None or not holds_a_version(existing, so):
        return None
    return _kept_at(existing, so)


def _kept_at(location: str, so: StorageOptions) -> CreateTableResponse:
    return CreateTableResponse(location=location, version=lance.dataset(location, storage_options=so, session=shared_lance_session()).version)


def _existing_location(ns: LanceNamespace, segments: list[str]) -> str | None:
    """The location the table is described at, or ``None`` if no table has the name; a declared table has one too.

    Measured on pylance 13.0.0, the `dir` backend describes a declared table without ``check_declared``. Whether the
    table holds a version is :func:`holds_a_version`'s to judge, never the backend's verdict ([[LH-528]]): a declared
    table has no readable dataset, so a caller must not open it.
    """
    try:
        resp = ns.describe_table(DescribeTableRequest(id=segments, with_table_uri=True))
    except TableNotFoundError:
        return None
    return getattr(resp, "table_uri", None) or getattr(resp, "location", None)


def rename_table(
    ns: LanceNamespace,
    source: table_claims.ResolvedTable,
    new_table_name: str,
    new_namespace_id: list[str] | None,
    *,
    so: StorageOptions,
    claims: location_claims.ClaimStore,
    root: str,
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
    fixes. Three failure modes go with
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

    THE LOCATION CLAIM IS THE ARBITRATION ([[LH-204]]). ``source`` is the table the request's name resolved to when
    it reached the door, its location and that location's claim as read then (ADR 0173 §7, [[LH-521]]). Before
    either backend call the claim is handed from the source id to the destination id under the ETag that read
    found (:mod:`service_kit.lakehouse.location_claims`), so of N concurrent renames of one source exactly one
    holds the location and the rest answer code 14, and so does a rename of a table a drop or deregister is
    acting on. Nothing in the backend can do this: measured on
    pylance 12.0.0 (lh204 m1), concurrent deregisters of one id all succeed and a register at an occupied
    location commits, so eight barrier-threaded renames left eight live ids on one dataset, 4 of 4 rounds.

    The two backend calls are not one transaction. A crash between them leaves the table reachable by
    no id with its claim naming the destination; a re-register of either id at the location, after the
    claim's lease, recovers it.
    """
    segments = source.segments
    if not new_table_name.strip():
        raise InvalidInputError("new_table_name is required")
    new_segments = rename_destination(segments, new_namespace_id, new_table_name)
    if new_segments == segments:
        raise InvalidInputError("the rename destination is identical to the source")
    source_uri = source.location
    if source_uri is None or not holds_a_version(source_uri, so):
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
    if _existing_location(ns, new_segments) is not None:
        raise TableAlreadyExistsError(f"table already exists: {'.'.join(new_segments)}")
    # RELATIVE, because the dir backend refuses an absolute URI here ("Absolute URIs are not allowed
    # for register_table"): the key the source's location decodes to under the namespace connection's
    # own root, ``root``, as both undrops re-attach a table (:func:`relative_location`, [[LH-528]]).
    # NO BRANCH GUARD, and its absence is a decision. A branch is a shallow clone referencing its
    # source root by ABSOLUTE path, so the byte-copy rename this door used to perform orphaned every
    # branch — it copied the root, deleted the source, and answered 200 over unreadable branch data.
    # The pointer move copies and deletes nothing: measured on the `dir` backend, `branches.list()`
    # after a rename returns the identical entry and the data reads back, because the bytes never
    # moved. Re-adding a refusal would decline a safe operation.
    location = relative_location(source_uri, root=root)
    # THE CLAIM IS HANDED OVER FIRST, and it is the whole of this door's race arbitration: the loser of
    # two renames of one source finds the claim changed since it read it, or reads the winner's claim, and answers
    # code 14 before touching the backend. The source is still retired before the destination is written, so a rename never shows
    # two ids on the dataset even for the instant between the two calls. The token makes this request's
    # claim its own: two renames into the SAME destination name one holder, and only the request that
    # stamped the claim may read it as its retry.
    source_id, dest_id = CATALOG_DELIMITER.join(segments), CATALOG_DELIMITER.join(new_segments)
    try:
        table_claims.hand_over(ns, claims, source, table=dest_id, segments=new_segments, previous=source_id, token=uuid.uuid4().hex)
    except (location_claims.LocationHeldError, location_claims.ClaimChangedError) as refused:
        holder = refused.claim.table if isinstance(refused, location_claims.LocationHeldError) else None
        log.warning("rename_refused_held_location", extra={"source": source_id, "location": source_uri, "holder": holder})
        raise ConcurrentModificationError(
            f"cannot rename {source_id}: its location is held by another table, by a concurrent rename or registration; describe it and retry"
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


def relative_location(location: str, *, root: str) -> str:
    """The key ``location`` decodes to under ``root``: what RegisterTable takes to re-attach the table there.

    Every re-registration makes this conversion, a rename's and both undrops'. The ``dir`` backend refuses an absolute
    location and keeps a relative one as the literal key it is given, while ``describe_table`` answers an absolute URL
    that percent-encodes a registered table's name (``…/r%C3%A4ksm%C3%B6rg%C3%A5s``). The store holds the table's bytes
    under the key that decodes to once (``…/räksmörgås``), so that key, whole, is what the table re-attaches at
    ([[LH-517]]). ``root`` is the root the namespace's connection is rooted at, its warehouse's for a bound namespace
    ([[LH-528]]): a location under another root has no key the backend resolves to it.

    Raises:
        InternalError: ``location`` names no place under ``root``, which no door attaches a table at.
    """
    under, base = decoded_segments(location), decoded_segments(root)
    if under is None or base is None or len(under) <= len(base) or under[: len(base)] != base:
        raise InternalError(f"the location {location!r} names no place under its namespace root {root!r}, so the table cannot be re-attached there")
    return "/".join(under[len(base) :])


def dataset_fs(uri: str, so: StorageOptions) -> tuple[pafs.FileSystem, str]:
    """Resolve ``uri`` to a ``(filesystem, path)`` pair for a dataset relocation.

    A local ``file://`` / bare-path location (the ``dir`` backend default) needs no credentials; an
    ``s3://`` location builds an ``S3FileSystem`` from the SAME storage options the datasets are opened
    with (path-style, http-ok endpoint) — mirroring the media head's filesystem so RustFS works.

    The path is the key pylance opens: ``describe_table`` answers a registered location holding a non-ASCII character
    or a ``%`` percent-encoded (``…/r%C3%A4ksm%C3%B6rg%C3%A5s``), the store keeps the key it decodes to once
    (``base_refs.decoded_path``), and pyarrow's S3 filesystem takes a path literally ([[LH-520]]). A local URI pyarrow
    decodes itself.
    """
    if uri.startswith("s3://") and so.get("endpoint"):
        return s3_filesystem(so), decoded_path(uri)
    resolved, path = pafs.FileSystem.from_uri(uri)
    return resolved, path


#: A committed manifest's name under either naming scheme: ``{version}.manifest`` (V1) or
#: ``{u64::MAX - version:020}.manifest`` (V2) (`lance_docs/file_format.md` § Manifest Naming Schemes). A staged
#: ``{version}.manifest-{uuid}`` (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md:292`) and a detached
#: ``d<id>.manifest`` name no version a reader resolves.
_COMMITTED_MANIFEST: Final = re.compile(r"[0-9]+\.manifest")


def holds_a_version(location: str, so: StorageOptions) -> bool:
    """Does the table at ``location`` hold a committed version, as the store keeps it? A table that does not is only declared.

    The catalog's own judgment ([[LH-528]]), at the key the location decodes to (:func:`dataset_fs`), for every door that
    asks whether a table is only declared. The `dir` backend probes ``base_path.join(location)``, and object_store's
    ``Path::join`` appends one segment, percent-encoding ``/`` and every non-ASCII byte, so for a table registered at
    ``outside/held`` or ``räksmörgås`` it probes a key that does not exist and answers that the table has no storage
    (``rust/lance-namespace-impls/src/dir/manifest.rs:2272`` at lance v13.0.0; measured on pylance 13.0.0 on a local root
    and on S3). The backend also counts any object under ``_versions/`` as storage, so it answers that a declared table
    whose first commit was staged and never finalized has storage; only a committed manifest's name counts here.
    """
    fs, path = dataset_fs(location, so)
    listed = fs.get_file_info(pafs.FileSelector(f"{path.rstrip('/')}/_versions", allow_not_found=True))
    return any(info.type == pafs.FileType.File and _COMMITTED_MANIFEST.fullmatch(info.base_name) for info in listed)


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


def verify_fragment_data_files(location: str, so: StorageOptions, fragments: list[dict[str, Any]]) -> None:
    """Reject a commit that references data files ABSENT under ``<location>/data/`` (audit HIGH fix).

    ``location`` is the ref's own directory (:func:`_ref_location`): a branch's appended files live under
    ``tree/<branch>/data/``.

    Each fragment's ``files[].path`` is a bare filename under the dataset's ``data/`` dir: a file naming a
    base was already refused by :func:`_refuse_based_data_files`. A missing file means the client's direct
    write targeted the wrong prefix — committing it would publish an unreadable version, so raise 400 and
    leave the table as-is. A declared ``file_size_bytes`` must be the object's size: Lance reads the footer
    at the declared size, so a larger one failed every read of the table (measured, [[LH-211]]). An absent
    size is legitimate, and Lance asks the store.
    """
    fs, base = dataset_fs(location, so)
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


def object_sizes(uris: Sequence[str], *, so: StorageOptions) -> list[int | None]:
    """Each object's size, ``None`` for one that is absent: one batched lookup per filesystem, as :func:`verify_fragment_data_files` does."""
    if so.get("endpoint") and all(uri.startswith("s3://") for uri in uris):
        infos = s3_filesystem(so).get_file_info([uri.removeprefix("s3://") for uri in uris])
    else:
        infos = [fs.get_file_info(path) for fs, path in (dataset_fs(uri, so) for uri in uris)]
    return [None if info.type != pafs.FileType.File else int(info.size) for info in infos]


def object_store_path(location: str) -> str:
    """``location`` as Lance's object store names it: no scheme, no bucket, no leading slash, decoded once as pylance opens it."""
    decoded = decoded_path(location)
    return decoded.partition("/")[2] if location.startswith("s3://") else decoded


#: How many times a stamped commit plans again after another writer took the version it followed.
_STAMPED_COMMIT_ATTEMPTS: Final = 10


def commit_stamped[T](
    table: table_claims.DoorTable, so: StorageOptions, properties: dict[str, str], plan: Callable[[lance.LanceDataset], tuple[Transaction, T]]
) -> tuple[int, T]:
    """Commit the transaction ``plan`` builds on main's latest version with ``properties``; return the version.

    ``plan`` also returns what it measured (a merge's row counts), handed back beside the version. A commit through
    ``LanceDataset.commit`` would rebase over a writer that landed first, and a merge_insert transaction loses its key
    check on the way through Python (pylance 13.0.0: ``LanceOperation.Update`` carries no inserted-rows filter):
    measured, two racing ``when_matched_fail`` inserts of one key both landed, with ``max_retries=0`` as well. So the
    transaction is committed detached and finalized as exactly the version after the one it was planned on, through
    the backend's put-if-not-exists (the ``version/create`` finalize); a lost race plans again on the new latest
    version, whose rows the key check then sees.

    The finalize names the table and no location, so it is the door's guarded native write ([[LH-530]]), asking the name
    again after the plan has written its data files, which can take as long as the rows do.

    Raises:
        ConcurrentModificationError: Every attempt lost its race.
        TableChangedError: The table changed since the door's resolution.
    """
    table_id = table.segments
    for _ in range(_STAMPED_COMMIT_ATTEMPTS):
        dataset = disarm_commit_path_cleanup(table.open(so), table_id)
        transaction, measured = plan(dataset)
        detached = lance.LanceDataset.commit(dataset, replace(transaction, transaction_properties=properties), detached=True)
        location = dataset.uri
        version = dataset.version + 1
        finalize_id = manifest_bytes.detached_id()
        try:
            fs, base = dataset_fs(location, so)
            versions = f"{base.rstrip('/')}/_versions"
            with fs.open_input_stream(f"{versions}/d{detached.version}.manifest") as source:
                body = manifest_bytes.with_manifest_version(source.read(), version)
            with fs.open_output_stream(f"{versions}/d{finalize_id}.manifest") as sink:
                sink.write(body)
            request = CreateTableVersionRequest(
                id=table_id, version=version, manifest_path=f"{object_store_path(location)}/_versions/d{finalize_id}.manifest", manifest_size=len(body)
            )
            try:
                table.write_natively("create_table_version", request)
            except table_claims.TableChangedError:
                raise
            except ConcurrentModificationError:
                discard_detached_manifest(location, so, finalize_id)
                continue
        except BaseException:
            discard_detached_manifest(location, so, finalize_id)
            raise
        finally:
            discard_detached_manifest(location, so, detached.version)
        return version, measured
    raise ConcurrentModificationError(f"the commit lost the race for the next version {_STAMPED_COMMIT_ATTEMPTS} times; retry it")


def discard_detached_manifest(location: str, so: StorageOptions, version: int) -> None:
    """Delete ``_versions/d<version>.manifest``, writing nothing else; the ``d`` prefix keeps the name apart from every
    manifest on the version line."""
    delete_table_object(location, so, f"_versions/d{version}.manifest")


def delete_table_object(location: str, so: StorageOptions, relative: str) -> None:
    """Delete ``<location>/<relative>``, writing nothing else.

    S3 goes through the storage seam's plain DeleteObject: pyarrow's ``S3FileSystem.delete_file`` PUTs the parent's
    directory marker after the delete, which left a ``_versions/`` object beside the table's manifests (measured on
    moto). A delete that fails leaves an inert object, never a wrong table, so it is logged rather than raised.
    """
    try:
        if location.startswith("s3://") and so.get("endpoint"):
            bucket, prefix = split_s3_uri(location.rstrip("/"))
            _object_client(so).delete_object(Bucket=bucket, Key=f"{prefix}/{relative}")
        else:
            fs, base = dataset_fs(location, so)
            fs.delete_file(f"{base.rstrip('/')}/{relative}")
    except Exception:
        log.warning("catalog_table_object_left", extra={"location": location, "object": relative}, exc_info=True)


def _object_client(so: StorageOptions) -> Any:  # noqa: ANN401 — a boto3 S3 client
    """The storage seam's S3 client on the catalog's own options, for an object no Lance write puts or deletes."""
    access_key, secret_key, session_token = credential_of(so)
    return s3_client(so["endpoint"], access_key=access_key, secret_key=secret_key, session_token=session_token, region=so.get("region"))


#: The Lance table-layout directory a dataset holds; a location holding objects and none under it is no dataset.
_VERSIONS_DIR: Final = "_versions"

#: How many list-and-delete passes a destroy makes before it reports what is left. A pass deletes every object it
#: listed, so a second pass finds only what a writer put meanwhile.
_DELETE_PASSES: Final = 3

#: The most keys one S3 DeleteObjects request takes.
_DELETE_BATCH: Final = 1000


class TableBytesLeftError(PartiallyApplied):
    """A destructive drop removed the table from ``__manifest`` and could not delete every object at its location."""


def destroy_table(
    ns: LanceNamespace,
    so: StorageOptions,
    segments: list[str],
    location: str,
    *,
    estate_roots: Sequence[str],
    table_uuid: str,
    namespace_uuid: str | None,
    control: location_claims.ClaimStore,
    dropped_by: str | None = None,
) -> DropTableResponse:
    """Destroy the table ``segments`` names, whose location its door resolved to ``location`` ([[LH-532]]).

    DropTable deletes the table's data (``lance_docs/ns_catalog/spec.yaml:498``), and the data is where Lance opens it:
    at the key the location decodes to once (:func:`dataset_fs`), with the `dir` catalog's reservation marker under the
    location as spelled (``maintenance.services.purge.store_paths`` measures the split). The backend's own drop deletes
    ``base_path.join(location)``, which percent-encodes the location as one segment, so for a table registered at
    ``outside/held`` or ``räksmörgås`` it removes the row and deletes nothing (on a local root it raises after the row
    is gone; ``rust/lance-namespace-impls/src/dir/manifest.rs:3180-3211`` at lance v13.0.0). So the catalog keeps the
    backend's order, the row first and then the data: it judges the bytes, removes the row with DeregisterTable, which
    deletes nothing, and deletes the bytes itself with ``so``, its own session:

    1. Judged before anything changes: the location must name one place below a store root, outside every control
       prefix, and neither be nor contain an ``estate_roots`` entry; at its key it must hold nothing, a ``_versions/``
       tree, or the reservation marker alone (a declared table); as spelled, at most the marker. A refusal leaves the
       table registered and every byte in place. The table's own data directories on its data bases are read from its
       base record here too ([[LH-286]], :func:`service_kit.lakehouse.data_directories.directories_to_end`), since
       they are its bytes as much as its root is.
    2. A location holding a ``_versions/`` tree gets a residue trash record on ``control`` before the row goes
       (:func:`trash.make_record`'s ``residue``, grace 0), so bytes a failed delete leaves keep their location's claim
       (``table_claims.kept_in_trash``): no register adopts them, undrop refuses them, the purge reclaims them. A
       location holding at most the marker gets none and its claim lapses as usual: nothing opens a marker alone, so a
       register attaching it later adopts no data, and the purge refuses a location with no ``_versions/``, so a
       record there would hold the claim forever (both measured on pylance 13.0.0 over moto).
    3. The row goes: the commit point. A deregister that answers TableNotFound found no row, and that answer is
       raised unchanged. Any other failure is judged by whether the table is still described: if it is, nothing was
       removed and the failure is raised; if it is not, the row went and its answer was lost, so the drop goes on.
    4. Every object under both paths is deleted, absent counting as deleted, until a listing finds none, with
       ``_versions/`` last, so a delete that stops part-way leaves the marker the purge recognises a dataset by. On S3
       the delete is DeleteObjects on the listed keys, which writes no directory marker (pyarrow's delete PUTs one,
       :func:`delete_table_object`). Then every object in the table's data directories, by key; the caller forgets the
       base record that names them once this answers.
    5. The residue record is cleared, and the drop answers the row it removed, or the judged location when the
       deregister's answer was lost.

    Raises:
        InvalidTableStateError: The location is not one a drop may delete; nothing changed.
        ServiceUnavailableError: The table's base record or a data directory's claim could not be read; nothing changed.
        TableNotFoundError: The backend holds no such table; nothing changed.
        TableBytesLeftError: The row is gone and objects remain at the location or in a data directory, or the row
            removed named another location, whose bytes are left alone. The residue record stands.
    """
    stored, spelled = _table_paths(location)
    holds_a_dataset = _require_destroyable(location, so, stored, spelled, estate_roots=estate_roots)
    table = CATALOG_DELIMITER.join(segments)
    directories = _data_directories_of(control, location, table)
    residue = (
        trash.make_record(table, uuid=table_uuid, location=location, dropped_by=dropped_by, grace_days=0, namespace_uuid=namespace_uuid, residue=True)
        if holds_a_dataset
        else None
    )
    if residue is not None:
        trash.put(control.control_root, control.storage_options, residue)
    removed: DeregisterTableResponse | None
    try:
        removed = native.call(ns, "deregister_table", DeregisterTableRequest(id=segments))
    except TableNotFoundError:
        _clear_residue(control, residue)
        raise
    except Exception as exc:
        if _still_described(ns, segments):
            _clear_residue(control, residue)
            raise
        log.warning("drop_deregister_answer_lost", extra={"table": table, "location": location, "error": str(exc)[:300]})
        removed = None
    try:
        if removed is not None and decoded_path(str(removed.location or "")) != decoded_path(location):
            raise InternalError(f"the row removed for {table} named {removed.location!r}, not the location judged")
        _delete_everything_under(location, so, stored, spelled)
        data_directories.delete_directories(directories, so)
    except Exception as exc:
        raise _bytes_left(table, location, exc, held=residue is not None) from exc
    _clear_residue(control, residue)
    if removed is None:
        return DropTableResponse(id=segments, location=location)
    return DropTableResponse(id=removed.id, location=removed.location)


def _data_directories_of(control: location_claims.ClaimStore, location: str, table: str) -> list[base_registry.RecordedBase]:
    """The data directories a drop of ``table`` at ``location`` deletes with its root, read before anything changes.

    Raises:
        ServiceUnavailableError: The base record or a directory's claim could not be read.
    """
    registry = base_registry.BaseRegistry(control_root=control.control_root, storage_options=control.storage_options)
    try:
        return data_directories.directories_to_end(registry, location, table=table)
    except (base_registry.UnreadableBaseRecordError, ClientError, BotoCoreError, ValueError) as exc:
        log.error("drop_data_directories_unread", extra={"table": table, "location": location, "error": str(exc)[:300]})
        raise ServiceUnavailableError(f"the data directories of {table} could not be read, so nothing was dropped") from exc


def _clear_residue(control: location_claims.ClaimStore, residue: dict[str, Any] | None) -> None:
    """Clear the residue record a drop filed, once it no longer stands for bytes left. Never raises.

    A record that outlives its drop holds nothing up: it names a location that is empty or that a live table still
    holds, the purge refuses the second (still registered) and finishes the first as a no-op, and the next drop of the
    table overwrites it.
    """
    if residue is None:
        return
    try:
        trash.clear(control.control_root, control.storage_options, str(residue["uuid"]))
    except Exception as exc:  # noqa: BLE001 — the drop's outcome is decided; a stale record is logged, not raised
        log.error("drop_residue_record_not_cleared", extra={"table": residue.get("id"), "location": residue.get("location"), "error": str(exc)[:300]})


def _still_described(ns: LanceNamespace, segments: list[str]) -> bool:
    """Whether the backend still describes ``segments``; a describe that fails otherwise answers yes, since the row's fate is unknown."""
    try:
        native.call(ns, "describe_table", DescribeTableRequest(id=segments))
    except TableNotFoundError:
        return False
    except Exception:  # noqa: BLE001 — an unreadable table is one whose row may stand
        return True
    return True


def _bytes_left(table: str, location: str, cause: Exception, *, held: bool) -> TableBytesLeftError:
    """The answer for a drop whose row is gone while objects remain at its location, logged at ERROR with the location.

    ``held`` says a residue trash record keeps the location's claim for the objects; without one the location held at
    most the reservation marker, which nothing opens.
    """
    log.error("table_bytes_left_after_drop", extra={"table": table, "location": location, "error": str(cause)[:300], "residue_record": held})
    remedy = (
        "the table no longer resolves; a residue trash record keeps its location held, and the trash purge reclaims the objects"
        if held
        else "the table no longer resolves; its location held only the reservation marker, which no table opens, and its claim lapses"
    )
    return TableBytesLeftError(
        f"table {table} was removed from the catalog and objects remain at {location}",
        problem_extra={"table": table, "location": location, "removed_from_catalog": True, "bytes_left": True, "remedy": remedy},
    )


def _on_object_client(location: str, so: StorageOptions) -> bool:
    return location.startswith("s3://") and bool(so.get("endpoint"))


def _table_paths(location: str) -> tuple[str, str]:
    """The two paths a table recorded at ``location`` keeps objects under: its decoded key and its spelling.

    Each is a store path: ``bucket/key`` on S3, an absolute path on a local root (a ``file://`` location's path).
    """
    anchor = "/" if store_of(location) == "file" else ""
    return f"{anchor}{decoded_path(location)}", f"{anchor}{normalise(location)}"


def _objects_under(location: str, so: StorageOptions, path: str) -> list[str]:
    """Every object under ``path/``, as a store path; on S3 a directory marker is one."""
    if _on_object_client(location, so):
        bucket, _, key = path.partition("/")
        pages = _object_client(so).get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{key.rstrip('/')}/")
        return [f"{bucket}/{entry['Key']}" for page in pages for entry in page.get("Contents", [])]
    fs, _ = dataset_fs(location, so)
    listed = fs.get_file_info(pafs.FileSelector(path, recursive=True, allow_not_found=True))
    return [info.path for info in listed if info.type == pafs.FileType.File]


def _contents(path: str, objects: Iterable[str]) -> list[str]:
    """``objects`` named relative to ``path``, without directory markers, which hold nothing."""
    prefix = f"{path.rstrip('/')}/"
    return sorted(name for name in (obj.removeprefix(prefix) for obj in objects) if name and not name.endswith("/"))


def _require_destroyable(location: str, so: StorageOptions, stored: str, spelled: str, *, estate_roots: Sequence[str]) -> bool:
    """Refuse a drop whose location is not one table's bytes, before anything changes (:func:`destroy_table`, step 1).

    Answers whether the location holds a ``_versions/`` tree: a dataset, rather than nothing or a declaration's marker.

    Raises:
        InvalidTableStateError: The location names no single place, a store root, a control prefix or an estate root
            (or a place holding one), or it holds objects that are not a Lance table's.
    """
    segments = decoded_segments(location)
    if segments is None or (store_of(location) != "file" and len(segments) < 2):
        raise InvalidTableStateError(f"the table's location {location!r} names no single place below a store root, so the drop deletes nothing")
    if crossed := sorted(CONTROL_PREFIXES.intersection(segments)):
        raise InvalidTableStateError(f"the table's location {location!r} crosses the control prefix(es) {crossed}, so the drop deletes nothing")
    if any(root and location_in_store(location, root) for root in estate_roots):
        raise InvalidTableStateError(f"the table's location {location!r} holds an estate root, so the drop deletes nothing")
    held = _contents(stored, _objects_under(location, so, stored))
    holds_versions = any(name.split("/")[0] == _VERSIONS_DIR for name in held)
    if held and held != [RESERVED_MARKER] and not holds_versions:
        log.warning("drop_refused_not_a_dataset", extra={"location": location, "objects": held[:5], "count": len(held)})
        raise InvalidTableStateError(
            f"the table's location {location!r} holds {len(held)} object(s) and no {_VERSIONS_DIR}/, so it is not a Lance table and the drop deletes nothing"
        )
    if spelled != stored:
        stray = [name for name in _contents(spelled, _objects_under(location, so, spelled)) if name != RESERVED_MARKER]
        if stray:
            log.warning("drop_refused_foreign_objects", extra={"location": location, "objects": stray[:5], "count": len(stray)})
            raise InvalidTableStateError(
                f"the table's location {location!r}, as spelled, holds {len(stray)} object(s) beside its reservation marker, so the drop deletes nothing"
            )
    return holds_versions


def _delete_everything_under(location: str, so: StorageOptions, stored: str, spelled: str) -> None:
    """Delete every object under ``stored`` and ``spelled``, ``_versions/`` last, then prove none is left (:func:`destroy_table`, step 4).

    The table's ``_versions/`` tree goes after every other object, so a delete that stops part-way leaves a location
    the purge still recognises as a dataset (``maintenance.services.purge._require_a_dataset``) and can finish.

    Raises:
        OSError: An object remains after every pass, or the store refused a delete.
    """
    paths = sorted({stored, spelled})
    versions = f"{stored.rstrip('/')}/{_VERSIONS_DIR}/"
    for _ in range(_DELETE_PASSES):
        held = [obj for path in paths for obj in _objects_under(location, so, path)]
        if not held:
            return
        rest = [obj for obj in held if not obj.startswith(versions)]
        last = [obj for obj in held if obj.startswith(versions)]
        if _on_object_client(location, so):
            _delete_objects(so, rest)
            _delete_objects(so, last)
        else:
            fs, _ = dataset_fs(location, so)
            for obj in rest:
                with suppress(FileNotFoundError):
                    fs.delete_file(obj)
            for path in [versions.rstrip("/"), *paths]:
                with suppress(FileNotFoundError):
                    fs.delete_dir(path)
    left = [obj for path in paths for obj in _objects_under(location, so, path)]
    if left:
        raise OSError(f"{len(left)} object(s) remain under {location} after {_DELETE_PASSES} delete passes: {left[:5]}")


def _delete_objects(so: StorageOptions, objects: Sequence[str]) -> None:
    """DeleteObjects on S3 store paths (``bucket/key``); a key already absent is deleted. Any other error raises.

    Raises:
        OSError: The store refused a key.
    """
    client = _object_client(so)
    by_bucket: dict[str, list[str]] = {}
    for obj in objects:
        bucket, _, key = obj.partition("/")
        by_bucket.setdefault(bucket, []).append(key)
    for bucket, keys in by_bucket.items():
        for start in range(0, len(keys), _DELETE_BATCH):
            batch = keys[start : start + _DELETE_BATCH]
            answered = client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True})
            if refused := answered.get("Errors"):
                raise OSError(f"the store refused {len(refused)} delete(s) in {bucket}: {refused[:3]}")


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


#: The native query path carries `k` and `offset` as u32: MEASURED on pylance 12.0.0, `k=2**32` and
#: `offset=10**12` raise `OverflowError` inside the native call, which answered 500.
_U32_MAX: Final = 2**32 - 1
#: Rows read to estimate a variable-width column's width, when the projection carries one.
_WIDTH_SAMPLE_ROWS: Final = 16
#: What the native answer adds beyond the projection: `_distance` or `_score` (float32) and `_rowid` (u64).
_ANSWER_EXTRA_BYTES: Final = 16


def refuse_an_unbounded_result(dataset: lance.LanceDataset, *, k: int, offset: int | None, columns: object, max_bytes: int) -> None:
    """Refuse a native query whose answer could exceed ``max_bytes``, before the native backend reads a row ([[LH-247]]).

    The native backend materialises the whole answer in one buffer before the first byte is sent, so
    the only bound this door has is the row count it asks for. `k` has a minimum of 0 and no maximum in
    the spec (`spec.yaml` QueryTableRequest), and `k=0` answers EVERY row. MEASURED on pylance 12.0.0
    from outside a one-CPU catalog process, 164 MB table of 1024-dim float32 vectors on a moto store:
    `k=0` returned 169,290,994 bytes and peaked at 822,192 kB against the pod's 524,288 kB limit, and a
    68 MB answer (`k=16000`, 492 MB table) at 885,188 kB. A whole-table read belongs to `/changes` or a
    credential vend, which stream.

    `k` is capped by the projected row width: exact for fixed-width columns, and estimated from
    :data:`_WIDTH_SAMPLE_ROWS` rows of the projection when it carries a variable-width one. `offset` is
    not, so a reader pages a large read with a bounded `k` (`service_kit.lancekit.reader`): the rows an
    offset skips are streamed past, not held. MEASURED on the same table and process: `k=1000,
    offset=39000` (skipping 156 MB) peaked at 395,256 kB, beside 381,412 kB for `k=4000` with no offset.

    Raises:
        InvalidInputError: ``k`` is 0, ``k`` or ``offset`` exceeds u32, or ``k`` rows exceed ``max_bytes``.
    """
    skip = offset or 0
    if k == 0:
        raise InvalidInputError("k must be at least 1: k=0 asks for every row in one answer; read a whole table through /changes or a credential vend")
    for name, value in (("k", k), ("offset", skip)):
        if value > _U32_MAX:
            raise InvalidInputError(f"{name}={value} exceeds {_U32_MAX}, the largest row count a query can carry")
    row_bytes = _projected_row_bytes(dataset, columns)
    ceiling = max(1, max_bytes // row_bytes)
    if k > ceiling:
        raise InvalidInputError(
            f"k = {k} rows of about {row_bytes} bytes exceed this door's {max_bytes}-byte answer (at most {ceiling} rows); "
            "page with a smaller k and an offset, or read a whole table through /changes or a credential vend"
        )


def _projected_row_bytes(dataset: lance.LanceDataset, columns: object) -> int:
    """The in-memory bytes one native answer row of the request's ``columns`` holds, its own extra columns included.

    Exact for fixed-width columns; a variable-width column's width is sampled from the table.
    """
    names = getattr(columns, "column_names", None)
    aliases = getattr(columns, "column_aliases", None)
    projection: list[str] | dict[str, str] | None = names or aliases or None
    with caller_sql("invalid query columns"):
        schema = dataset.scanner(columns=projection, limit=1).projected_schema
        widths = [_fixed_width(field.type) for field in schema]
        variable = [i for i, width in enumerate(widths) if width is None]
        sampled = 0
        if variable:
            sample = dataset.scanner(columns=_narrowed(projection, schema, variable), limit=_WIDTH_SAMPLE_ROWS).to_table()
            sampled = -(-sample.nbytes // sample.num_rows) if sample.num_rows else 0
    return sum(width for width in widths if width is not None) + sampled + _ANSWER_EXTRA_BYTES


def _narrowed(projection: list[str] | dict[str, str] | None, schema: pa.Schema, positions: list[int]) -> list[str] | dict[str, str]:
    """``projection`` cut down to the output columns at ``positions``, in the form it was given.

    A projected schema lists its fields in the projection's order, so a position names the same column
    in both; the projection's own spelling (a nested field path, an alias) is what the scanner takes back.
    """
    if isinstance(projection, dict):
        items = list(projection.items())
        return dict(items[i] for i in positions)
    if projection:
        return [projection[i] for i in positions]
    return [schema.field(i).name for i in positions]


def _fixed_width(data_type: pa.DataType) -> int | None:
    """The in-memory bytes one value of ``data_type`` takes, or ``None`` when that depends on the value."""
    if pa.types.is_fixed_size_list(data_type):
        inner = _fixed_width(data_type.value_type)
        return None if inner is None else inner * data_type.list_size
    if pa.types.is_struct(data_type):
        widths = [_fixed_width(data_type.field(i).type) for i in range(data_type.num_fields)]
        return None if None in widths else sum(w for w in widths if w is not None)
    try:
        return max(1, data_type.bit_width // 8)
    except ValueError:
        return None


@contextmanager
def caller_sql(action: str) -> Generator[None]:
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


#: The two shapes a query-plane refusal of the caller's expression arrives in (measured on pylance 13.0.0, through the
#: `dir` backend): ``query_table`` raises a typed ``InternalError`` whose text is Lance's Debug form,
#: ``Failed to execute query: InvalidInput { source: "Error parsing statement: …", location: … }``, while
#: ``explain_table_query_plan`` and ``analyze_table_query_plan`` raise a bare ``RuntimeError`` carrying
#: ``Failed to explain query plan for table at '<location>': Invalid user input: …``.
_QUERY_INPUT_MARKERS = (_USER_INPUT_MARKER, "InvalidInput {")
#: A column the caller named that the table lacks, in either shape: Display ``No field named nope.`` and Debug
#: ``FieldNotFound { field: Column { relation: None, name: "nope" }``.
_QUERY_MISSING_FIELD = re.compile(r'No field named (?P<display>[^\s.]+)|FieldNotFound \{ field: Column \{ relation: [^,]*, name: "(?P<debug>[^"]+)"')
#: The caller's own text in the Debug shape: ``InvalidInput { source: "<message>", location: … }``.
_QUERY_DEBUG_SOURCE = re.compile(r'InvalidInput \{ source: "(?P<source>(?:[^"\\]|\\.)*)"')


@contextmanager
def caller_query(action: str) -> Generator[None]:
    """Answer a query-plane refusal of the caller's request as the caller's mistake ([[LH-287]]).

    A filter naming an absent column is the spec's 12 ``TableColumnNotFound`` (404), and any other expression Lance
    refuses (a syntax slip, a literal of the wrong type) is 13 ``InvalidInput`` (400); everything without one of
    Lance's input markers propagates untouched, so a store outage stays a 5xx. The detail names the column or carries
    Lance's message with its location prefix and Rust source suffix removed: the raw text names the table's storage
    location and Lance's build paths, so it is logged and never answered.
    """
    try:
        yield
    except (InternalError, RuntimeError, ValueError, OSError) as exc:
        message = str(exc)
        if not any(marker in message for marker in _QUERY_INPUT_MARKERS):
            raise
        log.info("caller_query_rejected", extra={"action": action, "error": message[:1000]})
        if (missing := _QUERY_MISSING_FIELD.search(message)) is not None:
            raise TableColumnNotFoundError(f"{action}: the table has no column {missing.group('display') or missing.group('debug')!r}") from exc
        if (debug := _QUERY_DEBUG_SOURCE.search(message)) is not None:
            detail = debug.group("source").replace('\\"', '"').replace("\\\\", "\\")
        else:
            detail = message.rsplit(_USER_INPUT_MARKER, 1)[-1]
        raise InvalidInputError(f"{action}: {clean_lance_message(detail.lstrip(': '))}") from exc


#: Lance's schema check on a write, raised as a bare `OSError`. A stable Rust error-variant prefix, not
#: a path: "Append with different schema: `s` should have type string but type was int64" for a type
#: mismatch, "…: fields did not match, missing=[], unexpected=[zz]" for a column the table lacks.
_WRITE_SCHEMA_MARKER = "append with different schema:"


@contextmanager
def _write_schema_errors() -> Generator[None]:
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
def _column_op(action: str, fields: Sequence[str] = ()) -> Generator[None]:
    """Translate a schema-evolution failure the CALLER caused into the spec's 4xx, not a 500.

    The sibling of :func:`caller_sql` for the four column ops (add / alter / drop / update_field_metadata).
    It is a separate translator rather than a widening of ``caller_sql`` because ``caller_sql``'s single
    ``Invalid user input`` marker test is load-bearing for update/delete and would miss half of these:
    ``drop_columns`` raises an unmarked ``ValueError`` for both of its user errors. Seven distinct client
    mistakes answered 500 ``InternalError`` with detail "Internal Server Error" before this (#101) — the
    lakehouse UI renders that detail verbatim, so a user who typed a wrong column name read that the server
    had broken.

    Classification, in order:

    * ``LanceNamespaceError`` — already typed (``TableBranchNotFound`` out of the door's open, our own
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


def update_table(table: table_claims.DoorTable, so: StorageOptions, req: UpdateTableRequest) -> UpdateTableResponse:
    """Apply SQL ``[path, expression]`` updates to matching rows."""
    table_id = table.segments
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
    dataset = disarm_commit_path_cleanup(table.open(so, branch=req.branch), table_id)
    with caller_sql("invalid update expression or predicate"):
        result = dataset.update(updates, where=req.predicate)
    # pylance's update() returns an UpdateResult TypedDict (a plain dict at runtime); the row count is
    # `num_rows_updated`. (The previous `getattr(result, "num_updated_rows", ...)` was wrong twice — attr
    # access on a dict + wrong key — so updated_rows was hard-wired to 0 on every successful update.)
    updated = result.get("num_rows_updated") if isinstance(result, dict) else None
    # The version off the SAME handle the update committed through — pylance advances it in place.
    return UpdateTableResponse(updated_rows=updated if updated is not None else 0, version=dataset.version)


def delete_from_table(table: table_claims.DoorTable, so: StorageOptions, req: DeleteFromTableRequest) -> DeleteFromTableResponse:
    """Delete rows matching the request predicate."""
    table_id = table.segments
    # Same omission as `update_table` above, and worse here: a wrong-target DELETE loses rows that were
    # never meant to be touched, and returns 200.
    refuse_an_unbounded_boolean_chain(req.predicate, field="predicate")
    dataset = disarm_commit_path_cleanup(table.open(so, branch=req.branch), table_id)
    with caller_sql("invalid delete predicate"):
        dataset.delete(req.predicate)
    return DeleteFromTableResponse(version=dataset.version)


def insert_into_table(
    table: table_claims.DoorTable,
    so: StorageOptions,
    req: InsertIntoTableRequest,
    data: bytes,
    *,
    max_bytes: int,
    transaction_properties: dict[str, str] | None,
) -> InsertIntoTableResponse:
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

    On a table declaring a primary key, rows repeating a key among themselves are refused, and an append
    lands through Lance's `merge_insert(...).when_matched_fail().when_not_matched_insert_all()`, refused
    when the table holds one of its keys as it commits ([[LH-243]], :mod:`catalog.services.primary_keys`).

    ``transaction_properties`` (a run, :mod:`catalog.core.run_context`) ride the commit, on main only.
    """
    _refuse_a_run_on_a_branch(transaction_properties, req.branch, door="insert")
    # Validated here as well as at the door's coercion: this hands pyarrow's buffers to Lance in-process,
    # and Lance writes whatever an unvalidated offset points at.
    rows = read_arrow_body(data, max_bytes=max_bytes)
    dataset = disarm_commit_path_cleanup(table.open(so, branch=req.branch), table.segments)
    # THROUGH `InsertMode`, the one vocabulary every caller shares. pylance's own parser is not the
    # spec's: it takes `create`, which the spec does not give this door, and refuses an unknown value with
    # a bare ValueError that would answer 500. The parse is idempotent, so a mode the door already parsed
    # passes through unchanged.
    mode = InsertMode.parse(req.mode)
    keys = primary_keys.key_columns(dataset.lance_schema)
    primary_keys.refuse_repeats_within(rows, keys, door="insert")
    with _write_schema_errors():
        keyed_append = mode is InsertMode.APPEND and primary_keys.joinable(rows, keys)
        if keyed_append and transaction_properties is not None:
            version, _ = commit_stamped(table, so, transaction_properties, lambda latest: primary_keys.plan_new_keys(latest, rows, keys, door="insert"))
        elif keyed_append:
            # Lance's own enforcement, on the same handle: refused when the key is held as it lands, and
            # re-planned when another commit wins the race, so a racing insert of the same key is refused ([[LH-243]]).
            primary_keys.append_new_keys(dataset, rows, keys, door="insert")
            version = int(dataset.version)
        else:
            # An append or overwrite commutes with any racing append, so Lance's own rebase keeps it whole.
            dataset.insert(rows, mode=mode.value, transaction_properties=transaction_properties)
            version = int(dataset.version)
    # The rows this request wrote, read off its own payload: a row count diff across the commit would
    # include whatever a concurrent writer appended in between.
    return InsertIntoTableResponse(version=version, num_inserted_rows=rows.num_rows)


def _refuse_a_run_on_a_branch(transaction_properties: dict[str, str] | None, branch: str | None, *, door: str) -> None:
    """Refuse a run on a branch write: the backend's put-if-not-exists that keeps a stamped commit whole finalizes main only."""
    if transaction_properties is not None and recorded_branch(branch) is not None:
        raise UnsupportedOperationError(f"{door} with a run in its Context is served on main only; a branch write cannot carry the run into its commit")


def merge_insert_into_table(
    table: table_claims.DoorTable,
    so: StorageOptions,
    req: MergeInsertIntoTableRequest,
    data: bytes,
    *,
    max_bytes: int,
    transaction_properties: dict[str, str] | None,
) -> MergeInsertIntoTableResponse:
    """Run the spec's merge-insert against the ref the request NAMES, on the handle opened at the door's location.

    In-process on every ref, main included: the native merge names the table and would resolve it again ([[LH-530]]),
    and it runs the same ``MergeInsertBuilder`` on the dataset it loads (lance-namespace-impls 13.0.0, ``dir.rs``),
    answering no transaction id either.

    Same defect and same severity as `insert_into_table`: verified live, a merge naming `work` applied
    its update to MAIN and left the branch untouched, reporting `num_updated_rows: 1` for a row it had
    not changed on the dataset the caller asked about.

    Every spec parameter has a `MergeInsertBuilder` method of the same name, so the mapping is
    transcription rather than interpretation — which is exactly why this door is HONOURED where `query`
    is refused. There is no vector search, no full-text query and no index-selection surface here to
    re-derive and get subtly wrong.

    ``transaction_properties`` (a run, :mod:`catalog.core.run_context`) ride the commit, on main only; the merge then
    runs in-process, because the native merge takes no transaction properties.
    """
    _refuse_a_run_on_a_branch(transaction_properties, req.branch, door="merge_insert")
    # BEFORE THE MERGE. Lance in-process is handed pyarrow's buffers, where an offset past its values
    # buffer is written as process heap and a decreasing one crashes the process (measured on pylance 12.0.0),
    # so both filters answer 400 here.
    refuse_an_unbounded_boolean_chain(req.when_matched_update_all_filt, field="when_matched_update_all_filt")
    refuse_an_unbounded_boolean_chain(req.when_not_matched_by_source_delete_filt, field="when_not_matched_by_source_delete_filt")
    rows = read_arrow_body(data, max_bytes=max_bytes)
    dataset = disarm_commit_path_cleanup(table.open(so, branch=req.branch), table.segments)
    # A source repeating a key either inserts every copy (unmatched) or is ambiguous (matched); both arms ([[LH-243]]).
    primary_keys.refuse_repeats_within(rows, primary_keys.key_columns(dataset.lance_schema), door="merge_insert")
    # THE BUILDER IS CONSTRUCTED INSIDE THE GUARD, and that placement is the fix rather than a tidy-up:
    # `merge_insert(on)` is where Lance rejects a key column that does not exist, and it sat outside
    # `caller_sql`, so the one door whose whole job is matching on that column reported `Internal 18`
    # for naming it wrongly — while the branchless path answered 13.
    with _write_schema_errors(), caller_sql("invalid merge_insert filter"):
        if transaction_properties is not None:
            version, stats = commit_stamped(table, so, transaction_properties, lambda latest: _merge_builder(latest, req).execute_uncommitted(rows))
        else:
            stats = _merge_builder(dataset, req).execute(rows)
            version = dataset.version
    counts = stats if isinstance(stats, dict) else {}
    return MergeInsertIntoTableResponse(
        version=version,
        num_updated_rows=int(counts.get("num_updated_rows", 0)),
        num_inserted_rows=int(counts.get("num_inserted_rows", 0)),
        num_deleted_rows=int(counts.get("num_deleted_rows", 0)),
    )


def _merge_builder(dataset: lance.LanceDataset, req: MergeInsertIntoTableRequest) -> lance.MergeInsertBuilder:
    """The merge ``req`` asks for, on ``dataset``; every spec parameter has a builder method of the same name.

    Read as ``spec.yaml`` states them: each ``*_filt`` is its clause's own condition, so a filter applies whether or not
    its flag is set, and ``when_not_matched_insert_all`` defaults to false. The ``dir`` backend's native merge reads an
    absent ``when_not_matched_insert_all`` as an insert (lance-namespace-impls 13.0.0, ``dir.rs``), which the spec's
    default is not.
    """
    builder = dataset.merge_insert(req.on)
    if req.when_matched_update_all or req.when_matched_update_all_filt is not None:
        builder.when_matched_update_all(req.when_matched_update_all_filt)
    if req.when_not_matched_insert_all:
        builder.when_not_matched_insert_all()
    if req.when_not_matched_by_source_delete or req.when_not_matched_by_source_delete_filt is not None:
        builder.when_not_matched_by_source_delete(req.when_not_matched_by_source_delete_filt)
    if req.use_index is not None:
        builder.use_index(req.use_index)
    return builder


def refuse_a_restore_that_arms_cleanup(target: lance.LanceDataset, *, version: int) -> None:
    """Refuse restoring ``target``, the version a restore would commit again, when its config carries ``lance.auto_cleanup.*`` keys ([[LH-245]]).

    A restore commits the restored version's config, and Lance's commit-path cleanup reads the manifest a
    commit produces (measured on pylance 12.0.0: disarming the latest version and then restoring an armed one
    re-armed the table and deleted versions in the restore's own commit). No disarm can precede that commit,
    because the keys arrive with it, so the door refuses rather than letting a restore delete history past
    every hold.

    Raises:
        InvalidInputError: The version to restore carries ``lance.auto_cleanup.*`` keys.
    """
    if armed := auto_cleanup.armed_keys(target.config()):
        raise InvalidInputError(
            f"restore refused: version {version} carries commit-path auto-cleanup config {armed}, and restoring it would delete "
            "table versions inside the restore's own commit. Version reclamation belongs to the maintenance sweep; restore a version without these keys"
        )


def restore(table: table_claims.DoorTable, so: StorageOptions, req: RestoreTableRequest) -> tuple[RestoreTableResponse, int]:
    """Commit ``req``'s ref at ``req.version`` again as its newest version: the spec's answer, and the version made.

    The spec's ``RestoreTable`` answers a transaction id and no version; the version is the handle's own after the
    restore. Shared by the restore door and a promotion's rejection, which restores main to the published version
    (ADR 0156 C1).

    The version is opened at the door's location, where a missing branch answers 22 and a version the ref lacks 11, the
    codes every other door mints, and restored through that handle: the native restore names the table and would
    resolve it again ([[LH-530]]), and it restores the dataset it loads the same way (lance-namespace-impls 13.0.0,
    ``dir.rs``). The handle advances to the version the restore made, on main and on a branch alike (measured on pylance
    13.0.0). Unchecked, because a restore reads no row.

    Raises:
        InvalidInputError: The version to restore would arm commit-path cleanup.
        TableChangedError: The table changed since the door's resolution.
    """
    target = table.open_unchecked(so, version=req.version, branch=req.branch)
    refuse_a_restore_that_arms_cleanup(target, version=req.version)
    target.restore()
    version = int(target.version)
    made = target.read_transaction(version)
    return RestoreTableResponse(transaction_id=made.uuid if made is not None else None), version


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


def count_rows(table: table_claims.DoorTable, so: StorageOptions, req: CountTableRowsRequest, *, data_ns: LanceNamespace) -> int:
    """Count rows on the ref the request NAMES — the read-side twin of `update_table`'s omission; main's native count runs on ``data_ns``.

    The upstream `count_table_rows` takes the whole request and answers from main whatever `branch`
    says, so a branch-scoped count returned a number that was correct for a dataset the caller did not
    ask about. Verified live 2026-08-31 against ground truth read from S3 with pylance: after deleting
    two of three rows on `work`, the branch held 1 and this door reported 3 — and a branch that had
    NEVER BEEN CREATED also reported 3, which is what proves the parameter was not read rather than
    mishandled.

    Spec-mandated on both counts. `CountTableRowsRequest.branch` is "Branch to target. When not
    specified, the main branch is used" (namespace.md), and the SDK states the general rule for a
    branch-scoped handle: "Reads and writes operate in the branch's context." An absent ref is error
    code 22 `TableBranchNotFound`, which the door's open already raises — so routing through it buys the
    404 as well as the correct answer.

    MAIN STAYS ON THE UPSTREAM PATH. A branchless count is the overwhelmingly common one and already
    correct; re-implementing it here would put a second definition of "count" in the estate for no
    defect, and any divergence in predicate dialect or version resolution would be ours to own. The native count
    names the table, so the answer is kept only when the name still resolves to the door's location ([[LH-530]]).
    """
    refuse_an_unbounded_boolean_chain(req.predicate, field="predicate")
    if req.branch is None:
        # PINNED TO THE VERSION JUDGED ([[LH-279]]): the native count opens the table inside Rust, so the
        # request carries the exact version whose bases were just checked.
        req.version = int(table.open(so, version=req.version).version)
        response = table.read_natively("count_table_rows", req, via=data_ns)
        if not isinstance(response, CountTableRowsResponse) or response.count is None:
            raise TypeError(f"count_table_rows must answer a CountTableRowsResponse with a count, got {type(response).__name__}: {response!r}")
        return response.count
    dataset = table.open(so, version=req.version, branch=req.branch)
    with caller_sql("invalid count predicate"):
        return int(dataset.count_rows(filter=req.predicate) if req.predicate else dataset.count_rows())


def add_columns(table: table_claims.DoorTable, so: StorageOptions, req: AlterTableAddColumnsRequest) -> AlterTableAddColumnsResponse:
    """Add columns computed from per-column SQL expressions."""
    table_id = table.segments
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
    dataset = disarm_commit_path_cleanup(table.open(so, branch=req.branch), table_id)
    with _column_op("add_columns", dataset.schema.names):
        dataset.add_columns(transforms)
    return AlterTableAddColumnsResponse(version=dataset.version)


def alter_columns(table: table_claims.DoorTable, so: StorageOptions, req: AlterTableAlterColumnsRequest) -> AlterTableAlterColumnsResponse:
    """Rename / re-type / change nullability of existing columns.

    A ``virtual_column`` alteration (a UDF-backed column) is declined as Unsupported (406, code 0), as ``add_columns``
    declines one: pylance's ``alter_columns`` takes a name, a type and a nullability and nothing that carries a UDF.
    """
    table_id = table.segments
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
            # data_type is a JsonArrowDataType dict; pylance needs a real pa.DataType, not the JSON dict.
            alteration["data_type"] = _json_arrow_to_pa_type(dt if isinstance(dt, dict) else dt.model_dump())
        alterations.append(alteration)
    dataset = disarm_commit_path_cleanup(table.open(so, branch=req.branch), table_id)
    provenance_guard.refuse_provenance_alter(dataset.schema, alterations)
    with _column_op("alter_columns", dataset.schema.names):
        # pylance accepts plain dict alterations at runtime; its stub types them as
        # AlterColumn (a TypedDict), which ty can't match from dict[str, object].
        dataset.alter_columns(*alterations)  # ty: ignore[invalid-argument-type]
    return AlterTableAlterColumnsResponse(version=dataset.version)


def drop_columns(table: table_claims.DoorTable, so: StorageOptions, req: AlterTableDropColumnsRequest) -> AlterTableDropColumnsResponse:
    """Drop the named columns from the table."""
    table_id = table.segments
    columns = list(req.columns or [])
    dataset = disarm_commit_path_cleanup(table.open(so, branch=req.branch), table_id)
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
    table: table_claims.DoorTable,
    so: StorageOptions,
    *,
    begin_version: int,
    end_version: int | None,
    kind: changes.ChangeKind,
    columns: list[str] | None = None,
    branch: str | None = None,
) -> Iterator[bytes]:
    """The rows that changed in ``(begin_version, end_version]``, Arrow FILE-framed (§ J4), handed out in pieces.

    ONE SNAPSHOT, the window's end. The dataset is opened AT ``end_version`` — at the head it read, for
    an open window — and every kind is answered from that handle, so a closed window answers the same set
    however far the table has moved on (`changes` module docstring, THE WINDOW IS ONE SNAPSHOT). Before
    any row is read the table must track row versions (`changes.require_row_versions`) and the window
    must hold no transaction the version columns cannot describe (:func:`_require_followable_window`).

    `inserted` and `updated` are scans with the predicate `services/changes.py` composes, which owns the
    two documented windows; this function does not know what a change is and must not learn — a second
    place that composes either is a second place it can drift from `file_format.md`. Its own scan rather
    than the query door's, because `QueryTableRequest` is a VECTOR model (`k` and `vector` required).

    `deleted` is not a predicate: the rows are gone from the scan, so Lance answers from the transaction
    range instead (`DatasetDelta.get_deleted_row_ids()`, a single `_rowid` column). The publication delta
    is insert-only while the cascade's writer hard-deletes with `when_not_matched_by_source_delete`, so
    without this a retracted row left the tier without a trace any consumer could follow. Served on
    demand rather than stamped into the publish event (owner decision, 2026-09-11): a deleted-row set is
    unbounded, and stamping it turns a large delete into a large message on the bus.

    FILE FRAMING, not stream: this answers `application/vnd.apache.arrow.file`, matching the query
    door, and a consumer reading one framing as the other fails at the first batch rather than
    degrading. THE PROJECTION IS PART OF THE ANSWER: Lance returns only data columns unless the version
    pseudo-columns are named, so a feed that passed the caller's `columns` through verbatim handed back
    changed rows with no version on them and no way to ask for the next window.

    IT YIELDS rather than returns, because the answer is sized by the DATA and this door has no bound
    to put on it — no `limit`, no page token, and none available: the version window is the only
    cursor a consumer has, and one version can carry the whole table. MEASURED 2026-09-22 (200k rows x
    256B, 58.4 MB of Arrow over the feed's five projected columns): the materialising form peaked at
    **147.6 MB of RSS for one call, 2.53x the payload**; yielding peaks at **60.1 MB, 1.03x**, and the
    wire output is byte-identical (58,410,370 both). RSS is the measurement because `tracemalloc`
    reports 0 MB for the scan and the encode, watching the Python heap while Arrow allocates elsewhere.

    YIELDING BOUNDS WHAT THIS FUNCTION HOLDS, NOT WHAT LANCE HOLDS. The scan itself runs under the
    process scan defaults the catalog sets at boot (`catalog.main.bound_lance_scans`, [[LH-247]]):
    Lance's own size a batch in ROWS (8,192, whatever their width) and buffer up to 2 GB of IO ahead of
    the consumer (`lance_docs/guide.md:3441-3462`), and on a 164 MB table of 1024-dim float32 vectors
    this feed peaked the catalog at 597,224 kB against its 524,288 kB limit (pylance 12.0.0, measured
    from outside a one-CPU catalog over a moto store). No scanner option is passed here, so the feed
    and the native backend's scans share one bound an operator can move in one place.

    Raises:
        InvalidInputError: an invalid window, or a predicate or projection Lance rejects.
        TableVersionNotFoundError: ``end_version`` is not a version of the ref.
        InvalidTableStateError: the table has no stable row ids, or the window spans a transaction the
            version columns cannot describe or whose record cannot be read.
    """
    changes.validate_window(begin_version=begin_version, end_version=end_version)
    label = ".".join(table.segments)
    dataset = table.open(so, version=end_version, branch=branch)
    changes.require_row_versions(dataset.has_stable_row_ids, table=label)
    end = int(dataset.version)
    spans = _window_spans(dataset, begin_version, end, branch)
    if begin_version > 0:
        _require_followable_window(spans, table=label)
    if kind == "deleted":
        # A window from version 0 names only rows the consumer never held, and `delta()` would need the
        # manifest of version 1, which maintenance cleanup removes from every table older than its
        # retention: that resynchronisation is answered empty rather than read.
        return _deleted_row_ids(spans) if begin_version > 0 else _arrow_file_chunks(_DELETED_SCHEMA, None, iter(()))
    predicate = changes.change_filter(begin_version=begin_version, end_version=end, kind=kind)
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


class _WindowSpan(BaseModel):
    """The part of a window one ref committed: versions ``(low, high]`` of ``handle``."""

    model_config = {"arbitrary_types_allowed": True}

    handle: lance.LanceDataset
    #: The ref ``handle`` is on, `None` for main — what `checkout_version((ref, v))` names.
    ref: str | None
    low: int
    high: int


def _window_spans(dataset: lance.LanceDataset, begin: int, end: int, branch: str | None) -> list[_WindowSpan]:
    """``(begin, end]`` split by the ref that committed each version, newest first.

    A branch's own versions start above its `parent_version`; everything at or below it is its parent's
    history, read on the parent's handle at that version (`checkout_version((parent_branch, v))`, where
    `None` is main; measured on pylance 12.0.0, nested branches included). The branch's first manifest
    carries the parent's version number and no transaction a write made, and `read_transaction` on it
    PANICS on 12.0.0 (`not yet implemented`), while the versions below it do not exist on the branch at
    all — so no span ever asks a branch for a version at or below its branch point.
    """
    spans: list[_WindowSpan] = []
    handle, high, name = dataset, end, recorded_branch(branch)
    while True:
        if name is None:
            spans.append(_WindowSpan(handle=handle, ref=None, low=begin, high=high))
            break
        listed = handle.branches.list().get(name) or {}
        parent = listed.get("parent_version")
        floor = int(parent) if isinstance(parent, int) else 0
        spans.append(_WindowSpan(handle=handle, ref=name, low=max(begin, floor), high=high))
        if begin >= floor:
            break
        parent_branch = listed.get("parent_branch")
        name = parent_branch if isinstance(parent_branch, str) else None
        handle, high = handle.checkout_version((name, floor)), floor
    return [span for span in spans if span.high > span.low]


def _require_followable_window(spans: list[_WindowSpan], *, table: str) -> None:
    """Refuse a window spanning a transaction the version columns cannot describe (`changes.unfollowable`).

    One `read_transaction` per version in the window, and the window's BEGIN manifest must still exist:
    `delta()` reads it for the `deleted` kind, so a window whose begin maintenance cleaned up is refused
    here for every kind alike rather than answered by the scans and failed by the delta. A failed read
    refuses the window rather than answering it: a cleaned-up version's manifest is gone (OSError,
    measured on 12.0.0), and a read that PANICS raises a pyo3 `PanicException`, which derives from
    BaseException and cannot be caught by name.

    Raises:
        InvalidTableStateError: a version in the window is unfollowable or unreadable.
    """
    for span in spans:
        for version in range(span.high, span.low, -1):
            try:
                transaction = span.handle.read_transaction(version)
            except BaseException as exc:  # noqa: BLE001 — a Rust PANIC is not an Exception
                if isinstance(exc, KeyboardInterrupt | SystemExit):
                    raise
                _raise_if_store_outage(exc, table=table)
                raise InvalidTableStateError(
                    f"the change window on table {table} cannot be followed: version {version}'s transaction is unreadable "
                    f"({type(exc).__name__}). Resynchronise from begin_version=0."
                ) from exc
            if (reason := changes.unfollowable(transaction)) is not None:
                raise InvalidTableStateError(
                    f"the change window on table {table} cannot be followed: version {version} {reason}, so the version columns "
                    "do not describe what changed. Resynchronise from begin_version=0."
                )
    if not spans:
        return
    lowest = spans[-1]
    try:
        lowest.handle.checkout_version((lowest.ref, lowest.low))
    except BaseException as exc:  # noqa: BLE001 — a Rust PANIC is not an Exception
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        _raise_if_store_outage(exc, table=table)
        raise InvalidTableStateError(
            f"the change window on table {table} cannot be followed: its begin version {lowest.low} is no longer retained "
            f"({type(exc).__name__}). Resynchronise from begin_version=0."
        ) from exc


def _raise_if_store_outage(exc: BaseException, *, table: str) -> None:
    """Raise 503 for a read the store did not answer, which says nothing about whether the window can be followed.

    Refusing it as unfollowable (409, "resynchronise from 0") would send a consumer to re-read the whole
    table because the store blinked.

    Raises:
        ServiceUnavailableError: ``exc`` is an object-store outage (`service_kit.lancekit.outage`).
    """
    if reads_as_store_outage(exc):
        raise ServiceUnavailableError(f"the object store did not answer reading the change window on table {table}") from exc


#: The deleted-row feed's schema: Lance's own answer is a single `_rowid` column.
_DELETED_SCHEMA: Final = pa.schema([pa.field("_rowid", pa.uint64())])


def _deleted_row_ids(spans: list[_WindowSpan]) -> Iterator[bytes]:
    """The `_rowid`s each span's ref deleted, Arrow FILE-framed.

    One `delta()` per span, because `delta()` on a branch refuses a begin below its branch point (the
    manifest is not there; measured on 12.0.0). Each is closed (`delta()` refuses an open window,
    "Must specify both with_begin_version and with_end_version"). The caller asks only for a window with
    a begin above 0, whose begin manifest `_require_followable_window` has already found.
    """
    readers = []
    with caller_sql("invalid deleted-row window"):
        for span in spans:
            readers.append(span.handle.delta(begin_version=span.low, end_version=span.high).get_deleted_row_ids())
        batches = (batch for reader in readers for batch in reader)
        first = next(batches, None)
    return _arrow_file_chunks(readers[0].schema if readers else _DELETED_SCHEMA, first, batches)


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
    table: table_claims.DoorTable, so: StorageOptions, values: dict[str, str | None], *, branch: str | None = None
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
    dataset = disarm_commit_path_cleanup(table.open(so, branch=branch), table.segments)
    result = dataset.update_schema_metadata(values)
    return filter_internal_metadata(result), int(dataset.version)


def coerce_insert_arrow(
    table: table_claims.DoorTable, so: StorageOptions, data: bytes, branch: str | None = None, *, max_bytes: int, overwrite: bool = False
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
    target = table.open(so, branch=branch).schema
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
        log.warning("insert_dropped_columns", extra={"table": table.segments, "columns": discarded})
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
    table: table_claims.DoorTable,
    so: StorageOptions,
    updates: list[dict[str, Any]],
    branch: str | None = None,
) -> UpdateFieldMetadataResponse:
    """Merge/replace per-field metadata for the given field paths."""
    field_updates = {u["path"]: dict(u.get("metadata") or {}) for u in updates if u.get("path")}
    replace = any(bool(u.get("replace")) for u in updates)
    dataset = disarm_commit_path_cleanup(table.open(so, branch=branch), table.segments)
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


def table_history(table: table_claims.DoorTable, so: StorageOptions, limit: int = 50) -> list[dict[str, Any]]:
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
    dataset = table.open_unchecked(so)
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
            log.info("txn_unreadable", extra={"table": table.segments, "version": version, "error": str(exc)})
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
        return InvalidInputError(f"invalid {kind} name {subject!r}: {clean_lance_message(message)}")
    if any(marker in message.lower() for marker in _REF_VERSION_MISSING_MARKERS):
        # Lance's text names the manifest's storage path, so the caller is told the version, never the text.
        return TableVersionNotFoundError(f"no such version for {kind} {name!r}")
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
def _ref_errors(kind: str, name: str, *, invalid_name: str | None = None) -> Generator[None]:
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
        log.info("ref_operation_refused", extra={"kind": kind, "ref": name, "error": str(exc)[:500]})
        raise translated from exc


def list_tags(table: table_claims.DoorTable, so: StorageOptions, req: ListTableTagsRequest) -> ListTableTagsResponse:
    """List the table's tags as ``{name: TagContents{version, manifest_size, branch}}``, paged by name (``req.limit``,
    ``req.page_token``): pylance lists every tag at once, so the page is cut here."""
    listed = table.open_unchecked(so).tags.list()
    page, next_token = paginate(sorted(listed), req.page_token, req.limit)
    tags: dict[str, dict[str, Any]] = {}
    for name, tag in ((name, listed[name]) for name in page):
        # pylance's Tag is a TypedDict (plain dict at runtime), so read by key.
        entry = tag if isinstance(tag, dict) else {"version": getattr(tag, "version", None)}
        tags[name] = {
            "version": entry.get("version"),
            "manifest_size": entry.get("manifest_size") or 0,
            "branch": entry.get("branch"),  # None for a tag on main (TagContents.branch is optional)
        }
    # model_validate coerces the inner dicts into TagContents (not exported to name directly).
    return ListTableTagsResponse.model_validate({"tags": tags, "page_token": next_token})


def _open_with_branch(table: table_claims.DoorTable, so: StorageOptions, branch: str | None) -> lance.LanceDataset:
    """Main's handle, once the ``branch`` a tag op names is one the table has ([[LH-406]]).

    pylance resolves ``(branch, version)`` as one reference and reports a missing branch as a missing version
    (``version ghost:1 does not exist``), so the branch is established by reading the table's branch list first.

    Raises:
        TableBranchNotFoundError: The table has no branch ``branch``.
    """
    dataset = table.open(so)
    if (named := recorded_branch(branch)) is not None and named not in dataset.branches.list():
        raise TableBranchNotFoundError(f"branch {named!r} not found")
    return dataset


def _tag_reference(branch: str | None, version: int | None) -> int | tuple[str | None, int | None] | None:
    """Map the spec's optional ``branch`` + ``version`` to a pylance tag ``reference``: a bare int resolves
    against the CURRENT branch (main), so a branch-scoped tag must pass the ``(branch, version)`` tuple."""
    return (branch, version) if branch is not None else version


def create_tag(table: table_claims.DoorTable, so: StorageOptions, req: CreateTableTagRequest) -> CreateTableTagResponse:
    """Tag the given table version with a name (honoring the request's optional ``branch``), never while a reclaim runs.

    Raises:
        ConcurrentModificationError: A reclaim holds the table's history ([[LH-308]]); no tag was written.
    """
    dataset = _open_with_branch(table, so, req.branch)
    with table.history_held(HistoryWork.REFS), _ref_errors("tag", req.tag):
        dataset.tags.create(req.tag, _tag_reference(recorded_branch(req.branch), req.version))
    return CreateTableTagResponse()


def get_tag_version(table: table_claims.DoorTable, so: StorageOptions, req: GetTableTagVersionRequest) -> GetTableTagVersionResponse:
    """Return the table version a tag points to (404 on an unknown tag)."""
    tags = table.open_unchecked(so).tags
    try:
        version = tags.get_version(req.tag)
    except ValueError as exc:  # pylance 8 raises ("Ref not found"), it does NOT return None
        raise TableTagNotFoundError(f"tag {req.tag!r} not found") from exc
    if version is None:  # kept for a future pylance that returns None instead
        raise TableTagNotFoundError(f"tag {req.tag!r} not found")
    # Echo the branch the tag lives on (None for main) so a non-main tag isn't reported as main.
    entry = tags.list().get(req.tag) or {}
    return GetTableTagVersionResponse(version=version, branch=entry.get("branch"))


def update_tag(table: table_claims.DoorTable, so: StorageOptions, req: UpdateTableTagRequest) -> UpdateTableTagResponse:
    """Move an existing tag to a new table version (honoring the request's optional ``branch``), never while a reclaim runs.

    Raises:
        ConcurrentModificationError: A reclaim holds the table's history ([[LH-308]]); the tag was not moved.
    """
    dataset = _open_with_branch(table, so, req.branch)
    with table.history_held(HistoryWork.REFS), _ref_errors("tag", req.tag):
        dataset.tags.update(req.tag, _tag_reference(recorded_branch(req.branch), req.version))
    return UpdateTableTagResponse()


def delete_tag(table: table_claims.DoorTable, so: StorageOptions, req: DeleteTableTagRequest) -> DeleteTableTagResponse:
    """Delete a tag from the table."""
    with _ref_errors("tag", req.tag):
        table.open_unchecked(so).tags.delete(req.tag)
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


def list_branches(table: table_claims.DoorTable, so: StorageOptions, req: ListTableBranchesRequest) -> ListTableBranchesResponse:
    """List the table's branches as ``{name: BranchContents}``, read from pylance ``ds.branches`` and paged by name.

    The native ``DirectoryNamespace`` 501s branch ops, but ``lance.LanceDataset`` implements them, so we
    back them in-process here exactly like tags. A ``Branch`` is a TypedDict (plain dict at runtime).
    """
    listed = table.open_unchecked(so).branches.list()
    page, next_token = paginate(sorted(listed), req.page_token, req.limit)
    branches: dict[str, dict[str, Any]] = {}
    for name, branch in ((name, listed[name]) for name in page):
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
    return ListTableBranchesResponse.model_validate({"branches": branches, "page_token": next_token})


def create_branch(table: table_claims.DoorTable, so: StorageOptions, req: CreateTableBranchRequest) -> CreateTableBranchResponse:
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
    # `main` is a ref every table has and no branch list names (`lance_docs/file_format.md` "Branch Metadata"),
    # so a source spelled `main` is main itself.
    req.from_branch = recorded_branch(req.from_branch)
    dataset = table.open(so)
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
        with table.history_held(HistoryWork.REFS), _ref_errors("branch", source, invalid_name=req.name):
            dataset.create_branch(req.name, _branch_reference(req))
    except OSError:
        if req.name in table.open(so).branches.list():
            raise TableBranchAlreadyExistsError(f"branch {req.name!r} already exists") from None
        raise
    return CreateTableBranchResponse()


def delete_branch(table: table_claims.DoorTable, so: StorageOptions, req: DeleteTableBranchRequest) -> DeleteTableBranchResponse:
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
    dataset = table.open_unchecked(so)
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
    # A TAG NAMING A VERSION ON ONE OF THEM pins it the same way ("Ref conflict error: Branch b is referenced by
    # tags [("t", 1)]", measured on pylance 13.0.0), so it is asked before the first delete too.
    pins = sorted(tag for tag, meta in dataset.tags.list().items() if (meta or {}).get("branch") in doomed)
    if pins:
        raise InvalidTableStateError(
            f"branch {req.name!r} cannot be deleted: tag {', '.join(repr(t) for t in pins)} names a version on it or on a branch inside it, and a "
            "tag pins its branch. Delete or move the tag first. Nothing was deleted"
        )
    deleted: list[str] = []
    for name in doomed:
        try:
            with _ref_errors("branch", name):
                dataset.branches.delete(name)
        except TableBranchAlreadyExistsError as exc:
            # The ref-conflict marker here is a pin made after the read above, not a name collision.
            raise InvalidTableStateError(
                f"deleting branch {req.name!r} stopped at {name!r}: a branch forked from it or a tag naming it since the check pins it. "
                f"Deleted before stopping: {deleted}"
            ) from exc
        deleted.append(name)
    return DeleteTableBranchResponse()


def _forked_from(name: str, forks: Sequence[str]) -> InvalidTableStateError:
    return InvalidTableStateError(
        f"branch {name!r} cannot be deleted: {', '.join(repr(f) for f in forks)} is forked from it or from a branch inside it, and a fork "
        "pins its parent. Delete the fork first. Nothing was deleted"
    )
