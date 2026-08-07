"""Operations implemented in-process via pylance.

The native ``DirectoryNamespace`` stubs several table data, schema, and tag
operations. These functions fill the gap: resolve the table's dataset via the
namespace, then perform the operation with pylance.

Most functions take ``(ns, storage_options, request)`` and return the typed ``lance_namespace`` response
model. Exceptions: ``update_field_metadata`` takes ``(ns, storage_options, table_id, updates)``; and
``create_table`` is the one facade that receives the raw Arrow-IPC ``data`` and writes it directly at file
format 2.2 with stable row ids — the durable row identity row-id lineage needs.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any

import lance
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
    DropTableRequest,
    GetTableTagVersionRequest,
    GetTableTagVersionResponse,
    InvalidInputError,
    LanceNamespace,
    LanceNamespaceError,
    ListTableBranchesRequest,
    ListTableBranchesResponse,
    ListTableIndicesRequest,
    ListTableTagsRequest,
    ListTableTagsResponse,
    ServiceUnavailableError,
    TableAlreadyExistsError,
    TableColumnNotFoundError,
    TableNotFoundError,
    TableTagNotFoundError,
    TableVersionNotFoundError,
    UnsupportedOperationError,
    UpdateFieldMetadataResponse,
    UpdateTableRequest,
    UpdateTableResponse,
    UpdateTableTagRequest,
    UpdateTableTagResponse,
)

from catalog.core.namespace import open_dataset
from catalog.services import native
from service_kit.lakehouse import blobs
from service_kit.lakehouse.objectfs import s3_filesystem
from service_kit.lakehouse.schema import SchemaFields, facet_fields


log = logging.getLogger(__name__)

StorageOptions = dict[str, str]


def _table_id(req: object) -> list[str]:
    """Return the request's table identifier, or raise if it is empty."""
    table_id = getattr(req, "id", None)
    if not table_id:
        raise InvalidInputError("table identifier is required")
    return table_id


def _version(ns: LanceNamespace, so: StorageOptions, table_id: list[str], branch: str | None = None) -> int:
    """Return the table's current dataset version after an in-place mutation.

    ``branch`` MUST be the one the mutation targeted: a branch has its own version sequence, so reading it
    off main would answer a branch write with main's (unchanged) version — a wrong number that looks right.
    """
    return open_dataset(ns, so, table_id, branch=branch).version


def read_version_and_schema(
    ns: LanceNamespace,
    so: StorageOptions,
    table_id: list[str],
    pin_version: int | None = None,
    branch: str | None = None,
) -> tuple[int | None, SchemaFields]:
    """The ``(version, schema-facet fields)`` pair for stamping lineage after a committed write.

    ONE dataset open serves both reads, so the version and the schema can never come from two different
    snapshots (two separate reopens let a concurrent writer land between them and attach version N+1's
    schema to version N's WROTE edge). ``pin_version`` opens the dataset AT the version the write's
    response reported, so the schema is exactly that version's; without it (ops whose response carries
    only a ``transaction_id`` — insert/index/restore/schema-metadata) both come from the current snapshot.

    ``branch`` must name the ref the write COMMITTED TO. Reading a branch write back off main pins the WROTE
    edge to main's version and main's schema — the lineage graph would then record the evolution as having
    happened on a version that never carried it.

    Entirely best-effort: the write is already committed, so a readback failure must degrade the lineage
    enrichment (``(pin_version, [])`` — versionless when unpinned), never fail the request.
    """
    try:
        dataset = open_dataset(ns, so, table_id, version=pin_version, branch=branch)
        version = pin_version if pin_version is not None else int(dataset.version)
    except Exception as exc:
        log.warning("lineage_readback_failed", extra={"table": table_id, "error": str(exc)})
        return pin_version, []
    try:
        return version, facet_fields(dataset.schema)
    except Exception as exc:
        log.warning("schema_facet_read_failed", extra={"table": table_id, "error": str(exc)})
        return version, []


def payload_schema_fields(data: bytes, table_id: list[str]) -> SchemaFields:
    """Schema-facet fields parsed straight from an Arrow-IPC request payload (no storage round trip).

    A create/Overwrite writes exactly this payload, so its schema — including the blob/vector field
    metadata ``facet_fields`` keys on — IS the new table's schema; re-opening the just-written dataset
    would cost a describe + object-store open for information already in memory. NOT valid for ExistOk
    (which may keep an existing table the payload never touched) — that path reads back pinned instead.
    ``table_id`` is logging context only. Best-effort (``[]`` on a parse failure): the schema facet is an
    enrichment, never a reason to fail the write.
    """
    try:
        return facet_fields(pa.ipc.open_stream(data).schema)
    except Exception as exc:
        log.warning("schema_facet_parse_failed", extra={"table": table_id, "error": str(exc)})
        return []


def create_table(
    ns: LanceNamespace,
    so: StorageOptions,
    segments: list[str],
    data: bytes,
    *,
    mode: str | None = None,
    properties: dict[str, str] | None = None,
    allow_external_blobs: bool = False,
    external_blob_bases: list[str] | None = None,
    data_bases: list[str] | None = None,
) -> CreateTableResponse:
    """Create a table from an Arrow-IPC payload — a direct file-format-2.2 write with stable row ids.

    Runs off the event loop (blocking pyarrow decode + Lance/S3 IO), so the endpoint stays a single delegated
    call. ``allow_external_blobs`` permits ``Blob.from_uri`` columns pointing ANYWHERE outside the dataset
    root (the blanket bypass); ``external_blob_bases`` is the safer allowlist — external pointers are accepted
    only under one of these registered bases, with the blanket bypass left off.

    ``data_bases`` (#3-B) spreads the table's fragments across N approved buckets (Lance multi-base). Empty
    (the default) → a single-base write.
    """
    # 2.2 + stable row ids are CREATE-TIME-ONLY (they cannot be added later), so EVERY create must take this
    # path — a plain table that skipped it would be permanently unable to carry the durable row identity
    # `row_id_lineage` needs (row-level provenance: model → dataset version → the exact source rows).
    return _create_table_direct(
        ns,
        so,
        segments,
        data,
        mode=mode,
        properties=properties,
        allow_external=allow_external_blobs,
        external_blob_bases=external_blob_bases or [],
        data_bases=data_bases,
    )


def _base_name(uri: str) -> str:
    """A stable, deterministic base NAME for a #3-B data-distribution base URI.

    ``target_bases`` references bases by name; deriving the name from the URI (not a random id) means a create
    and any later op agree byte-for-byte, and the manifest's registered-base names stay human-readable."""
    return uri.replace("s3://", "").strip("/").replace("/", "-") or "base"


def _write_blob(
    table: pa.Table,
    uri: str,
    so: StorageOptions,
    *,
    mode: str,
    allow_external: bool,
    external_blob_bases: list[str],
    data_bases: list[str] | None = None,
) -> lance.LanceDataset:
    """Write a table at file format 2.2 with stable row ids.

    ``allow_external`` opts into ``Blob.from_uri`` columns ANYWHERE outside the dataset root (blanket bypass);
    ``external_blob_bases`` registers approved base URIs so external pointers UNDER a registered base are
    accepted with the bypass left off — the safer allowlist posture (lance_docs/guide.md). ``data_bases``
    (#3-B) are approved DATA-distribution bases the fragments round-robin across (the Uber pattern).
    Bases register on a fresh CREATE; an overwrite reuses the bases the table registered at create."""
    is_create = mode == "create"
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
    # base_store_params: each base's object-store creds at RUNTIME (pylance does NOT persist these to the
    # manifest — verified against the write_dataset/dataset docstrings). INVARIANT: every allowlisted data
    # base MUST share the catalog's endpoint/creds — the READ path (open_dataset) passes only the top-level
    # storage_options, so a base needing DIFFERENT creds/endpoint would write OK but be unreadable. The
    # allowlist is operator-configured to hold this; see LANCE_MULTIBASE_DATA_BASES in core/config.py.
    base_store_params = {u: dict(so) for u in data_bases} if data_bases else None
    try:
        return lance.write_dataset(
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
        raise


def _create_table_direct(
    ns: LanceNamespace,
    so: StorageOptions,
    segments: list[str],
    data: bytes,
    *,
    mode: str | None,
    properties: dict[str, str] | None,
    allow_external: bool,
    external_blob_bases: list[str],
    data_bases: list[str] | None = None,
) -> CreateTableResponse:
    """Create a table at file format 2.2 with stable row ids — the ONLY create path (audit 2026-07-14).

    Serves EVERY create: 2.2 + stable row ids are create-time-only, so a table that skipped this path could
    never gain the durable row identity row-id lineage needs — see ``create_table``.

    Honours the create ``mode`` against a *written* table (``ExistOk`` keeps it, ``Overwrite`` replaces its
    data, ``Create`` conflicts). A *declared-only* table — one that exists in the namespace but was never
    written (a bare ``POST /declare``, or a declare→write that crashed before the write) — has no readable
    dataset, so every mode simply lands the first data version into its already-declared location; this keeps
    the multi-step create idempotent and crash-safe, and never opens a table that isn't there (which 500'd).
    A brand-new table is ``declare``-d to learn its canonical location, then written at
    ``data_storage_version="2.2"`` (lance_docs/guide.md — Version Compatibility). Any failed fresh write is
    rolled back with ``drop_table`` so the name stays retryable rather than stuck describable-but-unreadable.
    """
    table = pa.ipc.open_stream(data).read_all()
    normalized = (mode or "create").lower()
    existing, only_declared = _existing_location(ns, segments)

    if existing is not None and not only_declared:  # a written, readable table already lives here
        if normalized == "overwrite":
            dataset = _write_blob(
                table,
                existing,
                so,
                mode="overwrite",
                allow_external=allow_external,
                external_blob_bases=external_blob_bases,
                data_bases=data_bases,
            )
            return CreateTableResponse(location=existing, version=dataset.version, properties=properties)
        if normalized in ("existok", "exist_ok"):  # keep it untouched, just report its current version
            version = lance.dataset(existing, storage_options=so).version
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
) -> CreateTableResponse:
    """Write the blob table's first data version into an already-declared ``location``, rolling the declare
    back with ``drop_table`` on failure so the name stays retryable rather than stuck declared-but-unreadable.
    """
    try:
        dataset = _write_blob(
            table,
            location,
            so,
            mode="create",
            allow_external=allow_external,
            external_blob_bases=external_blob_bases,
            data_bases=data_bases,
        )
    except Exception:
        with suppress(Exception):  # best-effort rollback; re-raise the real write error
            ns.drop_table(DropTableRequest(id=segments))
        raise
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


def _refuse_rename_with_branches(source_uri: str, so: StorageOptions, segments: list[str]) -> None:
    """Refuse to rename a table that has BRANCHES (audit 2026-07-14 — Lance format conformance).

    Rename is a byte-copy of the dataset root + a namespace repoint, which is safe precisely because a Lance
    dataset's INTERNAL refs are relative. A branch is not internal: it is a shallow clone that references its
    source root by ABSOLUTE path. Copying the root and deleting the source therefore leaves every branch
    pointing at bytes that no longer exist — the branches are silently orphaned and their data is
    unreadable, while the rename returns 200.

    There is no cheap correct fix (the branch manifests would each have to be rewritten to the new root), so
    the honest behavior is to refuse: a 400 the caller can act on beats a 200 that quietly destroys their
    branches. Drop the branches first, or copy the table instead.

    A dataset we cannot open (or whose branch listing the backend does not support) is NOT treated as
    branch-free — that would fail OPEN into the exact silent-orphan case. It raises, so the rename stops.
    """
    try:
        # `.branches` is a Branches HANDLE, not an iterable — `list(ds.branches)` raises TypeError. Probed
        # against the installed pylance rather than assumed: the accessor is `.list()`.
        branches = lance.dataset(source_uri, storage_options=so).branches.list()
    except (ValueError, OSError) as exc:
        raise ServiceUnavailableError(f"cannot verify whether {'.'.join(segments)} has branches; refusing to rename: {exc}") from exc
    if branches:
        names = sorted(branches) if isinstance(branches, dict) else [str(b) for b in branches]
        raise InvalidInputError(
            f"cannot rename {'.'.join(segments)}: it has branches {names}. A branch is a shallow clone that "
            "references this table's root by ABSOLUTE path, so a rename (copy + delete source) would leave "
            "them pointing at deleted bytes — silently orphaning them. Drop the branches first."
        )


def rename_table(
    ns: LanceNamespace,
    so: StorageOptions,
    segments: list[str],
    new_table_name: str,
    new_namespace_id: list[str] | None,
) -> tuple[list[str], str]:
    """Rename a table IN-PROCESS (#5b) — the ``dir`` backend's ``rename_table`` is a hard 501, and Lance
    has no format-level rename because table rename is a namespace-layer remap, not a data-plane commit.

    A Lance dataset is self-contained under one root with RELATIVE internal refs, so a rename is a byte
    copy of the dataset root to the destination location + a repoint of the namespace: declare the
    destination to learn its canonical location, relocate the data there, deregister the source. The copy
    preserves the full version history a read→rewrite would collapse to v1. Returns
    ``(new_segments, new_location)``. Raises ``TableNotFoundError`` (source missing / declared-only) or
    ``TableAlreadyExistsError`` (destination already a written table) so the endpoint maps 404 / 409.

    Data-safe within a single call, but NOT crash-atomic (documented limitation): a crash BETWEEN the copy
    and the source delete leaves the data under BOTH ids — a duplicate, never data loss — and the retry
    then 409s on the now-written destination until an operator drops one copy. The atomic fix is to copy
    into a staging path and publish it as the final step (future). No in-call failure loses data: a copy
    failure drops the half-copy (source intact + name free), and a source-delete failure orphans the source
    bytes for the reconcile sweep (the data is already safe at the destination — never roll it back).
    """
    # A blank/whitespace name would declare the identifier ``['']``, byte-copy the dataset to
    # ``<root>/.lance`` and DESTROY the named source — all while returning 200 (audit 2026-07-14).
    if not new_table_name.strip():
        raise InvalidInputError("new_table_name is required")
    dest_parent = list(new_namespace_id) if new_namespace_id else segments[:-1]
    new_segments = [*dest_parent, new_table_name]
    if new_segments == segments:
        raise InvalidInputError("the rename destination is identical to the source")
    source_uri, source_only_declared = _existing_location(ns, segments)
    if source_uri is None or source_only_declared:
        # Missing OR declared-but-unwritten → no dataset to relocate (404, symmetric with every op that
        # requires a written table).
        raise TableNotFoundError(f"table not found: {'.'.join(segments)}")
    _refuse_rename_with_branches(source_uri, so, segments)
    # The destination must be free in EVERY form. A declared-only stub is TAKEN, never adopted: reusing it
    # would skip ``declare_table`` — the only ATOMIC arbiter — so two concurrent renames into the same
    # declared name would both copy into one location and both retire their sources, silently destroying a
    # table (reproduced 9/10 under load, audit 2026-07-14). A stub from a crashed rename is cleared with an
    # explicit drop, never implicitly reused. (This pre-check is only a fast path — ``declare_table`` below
    # is the real guard, so a rename racing us for the same name loses there and surfaces a clean 409.)
    dest_uri, _dest_only_declared = _existing_location(ns, new_segments)
    if dest_uri is not None:
        raise TableAlreadyExistsError(f"table already exists: {'.'.join(new_segments)}")
    location = ns.declare_table(DeclareTableRequest(id=new_segments)).location
    if not location:
        raise InvalidInputError("namespace did not return a location for the rename destination")
    try:
        _copy_dataset(source_uri, location, so)
    except Exception:
        # The COPY failed: the destination is a partial half-copy and the SOURCE is untouched, so dropping
        # the half-copy is safe — it leaves the source intact and the destination name free (retryable).
        with suppress(Exception):
            ns.drop_table(DropTableRequest(id=new_segments))
        raise
    # The copy SUCCEEDED — the destination now holds a COMPLETE copy and is authoritative. From here the
    # destination must NEVER be rolled back: the source delete is NON-ATOMIC (S3 batches DeleteObjects; a
    # local walk can fail mid-directory), so a partial delete can leave the source unreadable — and dropping
    # the destination on that error would lose the data the copy just secured, recoverable NOWHERE (audit
    # 2026-07-14: strictly worse than the swallowed-error bug it replaced). A source-delete failure instead
    # ORPHANS the source bytes for the reconcile sweep; the rename still succeeds because the data is safe.
    try:
        _delete_dataset(source_uri, so)
    except Exception as exc:
        log.warning(
            "rename_source_bytes_orphaned",
            extra={"source": ".".join(segments), "dest": ".".join(new_segments), "error": str(exc)},
        )
    # Backends that keep a namespace pointer SEPARATE from the bytes retire it here; on ``dir`` the delete
    # above already did (so this raises TableNotFound). The destination is already published and correct, so
    # a failure here is a stale-pointer ops cleanup, not a failed rename.
    try:
        ns.deregister_table(DeregisterTableRequest(id=segments))
    except TableNotFoundError:
        pass
    except Exception as exc:
        log.warning(
            "rename_source_pointer_retained",
            extra={"source": ".".join(segments), "error": str(exc)},
        )
    return new_segments, location


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


def _copy_dataset(source_uri: str, dest_uri: str, so: StorageOptions) -> None:
    """Byte-copy a self-contained Lance dataset root ``source_uri`` → ``dest_uri``, preserving ALL versions
    (a read→rewrite would collapse the history to v1). ``copy_files`` recurses the dataset directory."""
    src_fs, src_path = _dataset_fs(source_uri, so)
    dst_fs, dst_path = _dataset_fs(dest_uri, so)
    pafs.copy_files(src_path, dst_path, source_filesystem=src_fs, destination_filesystem=dst_fs)


def _delete_dataset(uri: str, so: StorageOptions) -> None:
    """Delete a dataset root AUTHORITATIVELY — only an ALREADY-ABSENT root is tolerated.

    Deliberately NOT best-effort: on the shipped ``dir`` backend ``deregister_table`` is a no-op, so this
    delete IS the table's retirement. Swallowing a store error here would report a *successful* rename over
    a source that still resolves to half-deleted bytes — and ``pyarrow``'s ``ArrowIOError`` subclasses
    ``OSError``, which is exactly how a RustFS/S3 5xx used to be eaten silently (audit 2026-07-14). A real
    IO error must PROPAGATE so the caller can decide — ``rename_table`` orphans the source bytes for the
    reconcile sweep rather than rolling the destination back (the destination already holds the only copy).
    """
    fs, path = _dataset_fs(uri, so)
    # An ALREADY-ABSENT root is the retirement we wanted (idempotent on retry); every OTHER error — an S3
    # 5xx, a permission failure — propagates so the caller rolls the destination back.
    with suppress(FileNotFoundError):
        fs.delete_dir(path)


#: Lance commit OSError message markers (transaction.md conflict taxonomy). A schema/version mismatch can
#: NEVER succeed on retry (client error → 400); an incompatible transaction is a retryable concurrency
#: conflict (409, re-read + re-commit); anything ELSE — a RustFS 5xx surfaces as ``ArrowIOError``, itself an
#: ``OSError`` subclass — is a store OUTAGE (503), NOT a client conflict. Collapsing all three to 409 (the
#: naive design) would loop a doomed retry on a schema mismatch and mislabel an outage as contention.
_COMMIT_CLIENT_ERROR_MARKERS = ("different schema", "fields did not match", "same version", "invalid input")
#: NON-RETRYABLE per the format spec's conflict taxonomy (file_format.md, transaction.md: an *Incompatible*
#: conflict "fails with a non-retryable error"). This was previously lumped in with the retryable conflicts
#: below and answered with a 409 advising "re-read the table version and re-commit the fragments" — advice
#: that is actively DANGEROUS: after a concurrent Overwrite, the table's contents were REPLACED, so a client
#: obeying us would append fragments describing the OLD data into a semantically different table. Silent
#: corruption, recommended by our own error message. It is a client error: those fragments are now void and
#: the write must be redone against the current version, not re-committed.
_COMMIT_INCOMPATIBLE_MARKERS = ("incompatible transaction",)
#: RETRYABLE contention — the losing side of a genuine race can re-read and re-commit safely.
_COMMIT_CONFLICT_MARKERS = ("commit conflict", "concurrent")
#: An Append whose read_version has no committed base (a declared-only/never-written table, or a version
#: compacted away) — a CLIENT error (400), NOT a store outage (audit 2026-07-14): otherwise a client that
#: appends to a freshly-declared table (read_version=0) gets a 503 and retries the same version forever.
_COMMIT_NO_BASE_MARKERS = ("must already exist unless", "manifest was not found", "no such file")


def _classify_commit_error(exc: OSError) -> Exception:
    """Map a raised ``LanceDataset.commit`` ``OSError`` to the right catalog error (audit 2026-07-14)."""
    msg = str(exc).lower()
    if any(m in msg for m in _COMMIT_NO_BASE_MARKERS):
        return InvalidInputError(f"append target has no committed base version — create or overwrite the table first: {exc}")
    if any(m in msg for m in _COMMIT_CLIENT_ERROR_MARKERS):
        return InvalidInputError(f"fragments are incompatible with the table: {exc}")
    if any(m in msg for m in _COMMIT_INCOMPATIBLE_MARKERS):
        # NON-RETRYABLE (spec). Do NOT tell the caller to re-commit: the table changed underneath them
        # (e.g. a concurrent Overwrite), so these fragments describe data that no longer belongs. They must
        # re-WRITE against the current version. Advising a re-commit here is how you corrupt a table.
        return InvalidInputError(
            "commit is incompatible with the table's current transaction — this is NOT retryable: the "
            "table changed underneath these fragments (e.g. a concurrent overwrite). Discard them, re-read "
            f"the current version, and re-WRITE the data; do not re-commit: {exc}"
        )
    if any(m in msg for m in _COMMIT_CONFLICT_MARKERS):
        return ConcurrentModificationError(f"commit conflict — re-read the table version and re-commit the fragments: {exc}")
    return ServiceUnavailableError(f"object store unavailable during commit: {exc}")


#: The transaction-property marker a run's commit carries, `rask.ingest.run_id=<run_id>`. Rides
#: pylance's `commit_message` (stored as the `__lance_commit_message` transaction property —
#: verified round-trip on pylance 9.0.0: `read_transaction(v).transaction_properties` returns it
#: verbatim). The marker is what makes a commit REPLAY-RECOGNIZABLE: a retried commit for the same
#: run finds its own earlier version instead of appending twice.
_RUN_MARKER_PREFIX = "rask.ingest.run_id="


def _find_run_commit(location: str, so: StorageOptions, run_id: str, read_version: int) -> tuple[int, int] | None:
    """Did THIS run already commit? Scan versions after ``read_version`` for the run's marker.

    THE REPLAY THIS EXISTS FOR: the ingest `finalize` activity commits, then dies before Dapr records
    its result; the retry re-reads `read_version` fresh and — without this check — re-appends the same
    fragments as a brand-new version. Append never conflicts with Append (transaction.md), so nothing
    in the format refuses the duplicate; only recognizing our own commit can.

    Bounded by construction: the scan walks versions AFTER the retry's `read_version`, which in the
    replay case is at most a handful (our own commit plus whatever landed concurrently). A version
    whose transaction cannot be read (pre-transaction-file history, GC'd) is SKIPPED, not fatal —
    an unreadable stranger's version must not fail a legitimate first commit.
    """
    marker = _RUN_MARKER_PREFIX + run_id
    try:
        dataset = lance.dataset(location, storage_options=dict(so) if so else None)
    except Exception:
        return None  # no dataset yet -> certainly no prior commit by this run
    for version_info in dataset.versions():
        version = int(version_info["version"])
        if version <= read_version:
            continue
        try:
            transaction = dataset.read_transaction(version)
            props = getattr(transaction, "transaction_properties", None) or {}
        except Exception:
            continue
        if props.get("__lance_commit_message") == marker:
            rows = lance.dataset(location, version=version, storage_options=dict(so) if so else None).count_rows()
            return version, rows
    return None


def commit_appended_fragments(
    location: str, so: StorageOptions, fragments: list[dict[str, Any]], read_version: int, run_id: str | None = None
) -> tuple[int, int]:
    """Commit client-written fragments as an APPEND — the catalog as the governed commit coordinator (#2).

    The client wrote the data fragments DIRECTLY to object storage with vended, table-scoped creds
    (``lance.fragment.write_fragments`` — the guide's distributed-write protocol); this folds the tiny
    serialized ``FragmentMetadata`` into a metadata-ONLY Lance commit under ROOT creds, so no data byte ever
    transits the catalog and the version + lineage emit stay atomic (the External Manifest Store pattern —
    namespace.md). An Append INHERITS the dataset's ``data_storage_version`` + stable-row-id config from the
    manifest (``write_fragments`` reads it) and Lance assigns/rebases stable row ids at commit
    (row_id_lineage.md), so the 2.2 invariant needs no re-validation here — CREATE and OVERWRITE stay
    server-side to centralize it and to owner-govern the destructive reset. Append auto-rebases against
    concurrent appends and conflicts only with Overwrite/Restore (transaction.md); a stale/incompatible
    commit raises, mapped by :func:`_classify_commit_error`. Returns ``(version, row_count)``.
    """
    if not fragments:
        raise InvalidInputError("no fragments to commit")
    if read_version < 0:
        raise InvalidInputError(f"read_version must be non-negative, got {read_version}")
    # Client-controlled input: a malformed fragment dict raises KeyError/TypeError/ValueError from
    # ``from_json`` (outside the OSError taxonomy) — translate to a 400, never a 500 (audit 2026-07-14).
    try:
        frags = [lance.FragmentMetadata.from_json(json.dumps(f)) for f in fragments]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise InvalidInputError(f"malformed fragment metadata: {exc}") from exc
    # HIGH (audit 2026-07-14): Lance's commit validates NEITHER data-file existence NOR the declared row
    # count, so a client whose direct write landed under a DIFFERENT prefix than the catalog-resolved
    # location (no malice required) could otherwise commit a 200-OK-but-UNREADABLE current version that
    # breaks reads for EVERY reader until an operator restores. Pre-verify the files exist under the table
    # location; a failed check leaves the table untouched (400) instead of poisoning its current version.
    # IDEMPOTENT REPLAY (2026-08-07): a retried commit carrying a run_id is answered with the
    # version that run already committed — checked BEFORE the file-existence verification, because
    # a replay may arrive after maintenance compacted the staged files away, and refusing the
    # replay for missing files it no longer needs would fail a commit that already succeeded.
    if run_id:
        already = _find_run_commit(location, so, run_id, read_version)
        if already is not None:
            return already
    _verify_fragment_data_files(location, so, fragments)
    op = lance.LanceOperation.Append(frags)
    try:
        dataset = lance.LanceDataset.commit(
            location,
            op,
            read_version=read_version,
            storage_options=so,
            commit_message=(_RUN_MARKER_PREFIX + run_id) if run_id else None,
        )
    except OSError as exc:
        raise _classify_commit_error(exc) from exc
    return int(dataset.version), dataset.count_rows()


def _verify_fragment_data_files(location: str, so: StorageOptions, fragments: list[dict[str, Any]]) -> None:
    """Reject a commit that references data files ABSENT under ``<location>/data/`` (audit HIGH fix).

    Each fragment's ``files[].path`` is a bare filename under the dataset's ``data/`` dir (``base_id`` null →
    dataset root; a non-null ``base_id`` points into a registered external base we don't resolve here, so it
    is skipped rather than false-rejected). A missing file means the client's direct write targeted the
    wrong prefix — committing it would publish an unreadable version, so raise 400 and leave the table as-is.
    """
    fs, base = _dataset_fs(location, so)
    prefix = base.rstrip("/") + "/data/"
    missing: list[str] = []
    for frag in fragments:
        for data_file in (frag.get("files") if isinstance(frag, dict) else None) or []:
            if not isinstance(data_file, dict) or data_file.get("base_id") is not None:
                continue
            rel = data_file.get("path")
            if rel and fs.get_file_info(prefix + rel).type == pafs.FileType.NotFound:
                missing.append(str(rel))
    if missing:
        raise InvalidInputError(
            f"commit references {len(missing)} data file(s) not present under the table location (did the direct write target the wrong prefix?): {missing[:5]}"
        )


# Streaming window for the blob serving path: each chunk is one ``read_range`` call against
# storage, so the catalog never holds more than this per in-flight blob response (the read-side
# counterpart of the write-side ``BodySizeLimitMiddleware`` OOM guard).
_BLOB_CHUNK_BYTES = 8 * 1024 * 1024


@dataclass
class BlobStream:
    """One served blob read: the resolved window + a chunked reader, never the buffered payload.

    ``start``/``length`` are the RESOLVED window (``end`` derives the inclusive RFC 9110
    Content-Range position); ``ranged`` says whether the caller asked for a range (→ 206 +
    Content-Range) or the whole payload (→ 200); ``satisfiable=False`` means the requested range
    starts beyond the blob (→ 416 with ``bytes */size``). ``etag`` is the strong validator for the
    served ``(version, column, row)`` address. The private handle/dataset refs keep the lazy
    ``BlobFile`` (and the dataset it reads through) alive until the response finishes streaming.
    """

    size: int
    start: int
    length: int
    etag: str
    ranged: bool
    satisfiable: bool
    _handle: Any = None
    _dataset: Any = None

    @property
    def end(self) -> int:
        """Inclusive last byte position of the window (Content-Range form; meaningful when length > 0)."""
        return self.start + self.length - 1

    def chunks(self) -> Iterator[bytes]:
        """Yield the window in ``_BLOB_CHUNK_BYTES`` pieces — each piece one bounded ``read_range``.

        Blocking storage IO: the endpoint hands this to ``StreamingResponse``, which iterates sync
        generators in a threadpool (starlette ``iterate_in_threadpool``), so the event loop never
        blocks and at most one chunk per response is in memory.
        """
        offset, remaining = self.start, self.length
        while remaining > 0:
            step = min(_BLOB_CHUNK_BYTES, remaining)
            yield self._handle.read_range(offset, step)
            offset += step
            remaining -= step


def read_blob(
    ns: LanceNamespace,
    so: StorageOptions,
    table_id: list[str],
    *,
    column: str,
    row: int,
    version: int | None = None,
    range_spec: tuple[int | None, int | None] | None = None,
    if_range: str | None = None,
) -> BlobStream:
    """Resolve one blob payload (or a byte range of it) for the credential-less serving path (§9 P1).

    ``take_blobs`` opens the payload as a lazy ``BlobFile`` and :meth:`BlobStream.chunks` reads it in
    bounded ``read_range`` windows — the catalog never buffers the payload, so a multi-GB video is
    servable without the write-side OOM the body-limit middleware guards against. ``range_spec`` is
    the parsed Range header: ``(first, last)`` with ``last`` inclusive, ``(first, None)`` open-ended,
    ``(None, n)`` an RFC suffix range (the final ``n`` bytes). Resolution happens HERE (not in the
    endpoint) because a suffix range needs the blob's size, which only this dataset-open knows — one
    open serves the whole request. ``if_range`` is the raw ``If-Range`` validator: when it does not
    match this read's etag (``"<version>-<column>-<row>"``), the range is IGNORED and the full
    current payload served (RFC 9110 §13.1.5) — so a client resuming a download across an overwrite
    gets whole consistent bytes, never a silent splice of two incarnations.

    Guards, each a precise client error instead of the raw pylance panic it would otherwise be:
    unknown column → 404 ``TableColumnNotFoundError``; a column that exists but is not blob-typed →
    400 (``take_blobs`` would ValueError); ``row`` past the end → 400 (pylance's message is a
    row-address internals dump); a ``version`` with no manifest → 404 ``TableVersionNotFoundError``
    and a table whose declared location holds NO dataset (declared-only, or wiped storage) → 404
    ``TableNotFoundError`` (both are bare ValueErrors from lance, told apart by the manifest-path
    shape). A ZERO-LENGTH payload streams as an empty 200 — at pylance 8.0.0 a NULL blob is stored
    as a size-0 descriptor (probed: input ``null_count=1`` → stored ``null_count=0``), so null and
    ``b""`` are the same row state and ``take_blobs`` returns an EMPTY list for both (unguarded
    ``[0]`` would 500); any Range against it is unsatisfiable. Range resolution clamps ``last`` to
    the blob size (RFC 9110 §14.1.2) and reports ``first >= size`` as unsatisfiable rather than
    erroring (``read_range`` rejects over-length windows).
    """
    try:
        dataset = open_dataset(ns, so, table_id, version=version)
    except ValueError as exc:
        # lance raises bare ValueErrors for two distinct missing-things: a pinned version with no
        # manifest ("…/_versions/N.manifest was not found…") and a location with no dataset at all
        # ("Dataset at path … was not found…" — a declared-but-never-written table, or wiped
        # storage). Told apart by the manifest-path shape so neither is a 500 and neither is
        # mislabeled as the other; any other ValueError stays a 500.
        message = str(exc).lower()
        if version is not None and "_versions/" in message and ".manifest" in message:
            raise TableVersionNotFoundError(f"table version {version} was not found") from exc
        if "dataset at path" in message and "not found" in message:
            raise TableNotFoundError("table has no readable dataset at its declared location") from exc
        raise
    schema = dataset.schema
    if column not in schema.names:
        raise TableColumnNotFoundError(f"column {column!r} does not exist")
    if not blobs.is_blob_field(schema.field(column)):
        raise InvalidInputError(f"column {column!r} is not a blob column")
    rows = dataset.count_rows()
    if row >= rows:
        raise InvalidInputError(f"row {row} is out of range (table has {rows} rows)")
    etag = f'"{int(dataset.version)}-{column}-{row}"'
    if if_range is not None and if_range.strip() != etag:
        range_spec = None  # validator mismatch (or an If-Range date) → full current payload
    files = dataset.take_blobs(column, indices=[row])
    if not files:  # zero-length payload (null collapses to size-0 at write — see docstring)
        if range_spec is None:
            return BlobStream(size=0, start=0, length=0, etag=etag, ranged=False, satisfiable=True)
        return BlobStream(size=0, start=0, length=0, etag=etag, ranged=True, satisfiable=False)
    handle = files[0]
    size: int = handle.size()

    if range_spec is None:
        return BlobStream(
            size=size,
            start=0,
            length=size,
            etag=etag,
            ranged=False,
            satisfiable=True,
            _handle=handle,
            _dataset=dataset,
        )
    first, last = range_spec
    if first is None:  # suffix range: the final `last` bytes
        if last is None or last <= 0 or size == 0:
            return BlobStream(size=size, start=0, length=0, etag=etag, ranged=True, satisfiable=False)
        start = max(size - last, 0)
        end = size - 1
    else:
        if first >= size:
            return BlobStream(size=size, start=0, length=0, etag=etag, ranged=True, satisfiable=False)
        start = first
        end = size - 1 if last is None else min(last, size - 1)
    return BlobStream(
        size=size,
        start=start,
        length=end - start + 1,
        etag=etag,
        ranged=True,
        satisfiable=True,
        _handle=handle,
        _dataset=dataset,
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


def _clean_lance_message(message: str) -> str:
    """Lance's own text, minus the two things a caller must never receive.

    The ``Invalid user input`` prefix is replaced by our own action wording, and the Rust source location
    (``, /home/runner/work/lance/…/planner.rs:1019:20``) is trimmed: it is useful in a server log, noise in
    an API response, and it publishes the build path of a vendored library to every caller. Everything else
    survives — Lance frequently names the columns that DO exist, which is the whole value of the message.
    """
    body = message.split(_USER_INPUT_MARKER, 1)[1].lstrip(": ") if _USER_INPUT_MARKER in message else message
    return _RUST_SOURCE_SUFFIX.sub("", body).strip()


@contextmanager
def _user_sql(action: str) -> Iterator[None]:
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
        detail = _clean_lance_message(message)
        log.info("user_sql_rejected", extra={"action": action, "error": message})
        raise InvalidInputError(f"{action}: {detail}") from exc


#: A column op names a column that is not there. Lance says it four ways across three exception classes —
#: ``Column nope does not exist in the dataset`` (drop, ValueError), ``Invalid user input: Column "nope"
#: does not exist in the dataset`` (rename, OSError), ``Field 'nope' not found.\nAvailable fields: ['id']``
#: (update_field_metadata, OSError) — and the SPEC has a code for exactly this: 12 TableColumnNotFound (404).
#: Matched against pylance 9.0.0's real messages (probed, not guessed), so a pylance bump that rewords them
#: reds a test rather than silently reverting these paths to 500s.
_COLUMN_NOT_FOUND = re.compile(r"does not exist in the dataset|field '[^']*' not found", re.IGNORECASE)

#: Client mistakes Lance reports WITHOUT its ``Invalid user input`` marker. Both are refusals of a
#: well-formed request the caller got wrong, i.e. 400 — never a server fault.
_COLUMN_BAD_REQUEST = re.compile(r"cannot drop all columns", re.IGNORECASE)

#: Messages that already enumerate the schema for the caller; appending our own list would just repeat it.
_LISTS_FIELDS = re.compile(r"valid fields|available fields", re.IGNORECASE)


@contextmanager
def _column_op(action: str, fields: Sequence[str] = ()) -> Iterator[None]:
    """Translate a schema-evolution failure the CALLER caused into the spec's 4xx, not a 500.

    The sibling of :func:`_user_sql` for the four column ops (add / alter / drop / update_field_metadata).
    It is a separate translator rather than a widening of ``_user_sql`` because ``_user_sql``'s single
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
        detail = _clean_lance_message(message)
        if _COLUMN_NOT_FOUND.search(message):
            if fields and not _LISTS_FIELDS.search(detail):
                detail = f"{detail}. Valid fields are {', '.join(fields)}"
            log.info("column_op_rejected", extra={"action": action, "error": message})
            raise TableColumnNotFoundError(f"{action}: {detail}") from exc
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
    with _user_sql("invalid update expression or predicate"):
        result = open_dataset(ns, so, table_id).update(updates, where=req.predicate)
    # pylance's update() returns an UpdateResult TypedDict (a plain dict at runtime); the row count is
    # `num_rows_updated`. (The previous `getattr(result, "num_updated_rows", ...)` was wrong twice — attr
    # access on a dict + wrong key — so updated_rows was hard-wired to 0 on every successful update.)
    updated = result.get("num_rows_updated") if isinstance(result, dict) else None
    return UpdateTableResponse(updated_rows=updated if updated is not None else 0, version=_version(ns, so, table_id))


def delete_from_table(ns: LanceNamespace, so: StorageOptions, req: DeleteFromTableRequest) -> DeleteFromTableResponse:
    """Delete rows matching the request predicate."""
    table_id = _table_id(req)
    with _user_sql("invalid delete predicate"):
        open_dataset(ns, so, table_id).delete(req.predicate)
    return DeleteFromTableResponse(version=_version(ns, so, table_id))


def add_columns(ns: LanceNamespace, so: StorageOptions, req: AlterTableAddColumnsRequest) -> AlterTableAddColumnsResponse:
    """Add columns computed from per-column SQL expressions."""
    table_id = _table_id(req)
    columns = req.new_columns or []
    # The spec also allows a `virtual_column` (UDF/Docker-backed) instead of a SQL `expression`. That needs
    # a UDF execution backend we don't run, so reject it as a spec-correct 501 — not a 400 (it's a valid
    # request for an unsupported feature, not invalid input).
    if any(getattr(c, "virtual_column", None) is not None and not c.expression for c in columns):
        raise UnsupportedOperationError("virtual_column add_columns is not supported by this backend")
    transforms = {c.name: c.expression for c in columns if c.expression}
    if not transforms:
        raise InvalidInputError("add_columns requires a name and SQL expression per column")
    dataset = open_dataset(ns, so, table_id, branch=req.branch)
    with _column_op("add_columns", dataset.schema.names):
        dataset.add_columns(transforms)
    return AlterTableAddColumnsResponse(version=_version(ns, so, table_id, req.branch))


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
    with _column_op("alter_columns", dataset.schema.names):
        # pylance accepts plain dict alterations at runtime; its stub types them as
        # AlterColumn (a TypedDict), which ty can't match from dict[str, object].
        dataset.alter_columns(*alterations)  # ty: ignore[invalid-argument-type]
    return AlterTableAlterColumnsResponse(version=_version(ns, so, table_id, req.branch))


def drop_columns(ns: LanceNamespace, so: StorageOptions, req: AlterTableDropColumnsRequest) -> AlterTableDropColumnsResponse:
    """Drop the named columns from the table."""
    table_id = _table_id(req)
    dataset = open_dataset(ns, so, table_id, branch=req.branch)
    with _column_op("drop_columns", dataset.schema.names):
        dataset.drop_columns(list(req.columns or []))
    return AlterTableDropColumnsResponse(version=_version(ns, so, table_id, req.branch))


def read_schema_metadata(ns: LanceNamespace, so: StorageOptions, table_id: list[str]) -> dict[str, str]:
    """The table's schema-level metadata as ``{str: str}`` — the read twin of ``schema_metadata/update``.

    pylance 8.0.0's ``describe_table`` leaves ``DescribeTableResponse.metadata`` unpopulated, so the #74
    Table Properties UI could WRITE a property but never SEE it back (the editor always seeded empty — found
    by driving the real UI in a browser, 2026-07-21). The catalog fills the gap: read ``schema.metadata``
    (Arrow bytes keys/values → str) directly. Internal ``lineage.*`` bookkeeping keys are excluded — they're
    not user properties, and the update path MERGES, so hiding them never drops them on a UI save.
    """
    meta = open_dataset(ns, so, table_id).schema.metadata or {}
    out: dict[str, str] = {}
    for k, v in meta.items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        if key.startswith("lineage."):
            continue
        out[key] = v.decode() if isinstance(v, bytes) else str(v)
    return out


def update_schema_metadata(ns: LanceNamespace, so: StorageOptions, table_id: list[str], values: dict[str, str | None]) -> dict[str, str]:
    """Upsert the table's schema-level metadata; a ``None`` value DELETES that key.

    The table-level twin of :func:`update_field_metadata`'s dialect, and the only way to REMOVE a table
    property: the spec's ``update_table_schema_metadata`` merges (probed against a real ``dir`` backend —
    ``{owner}`` over ``{owner, tier}`` leaves ``tier`` standing, despite the spec text saying "Replace"),
    and its wire model types ``metadata`` as a strict ``{str: str}``, so a null cannot ride the native op
    at all. Omitting a key is therefore a no-op, which made the properties editor's remove button a silent
    lie until this existed.

    NEVER ``replace=True``. The map a caller holds came from :func:`read_schema_metadata`, which EXCLUDES
    the internal ``lineage.*`` keys — so replacing would silently drop the #21 coordinates that make the
    Lance file self-describing. Merge + explicit null-delete is the only shape that can't destroy them.

    Returns the table's new full map with ``lineage.*`` filtered out, matching what the read twin reports.
    """
    result = open_dataset(ns, so, table_id).update_schema_metadata(values)
    return {k: v for k, v in result.items() if not k.startswith("lineage.")}


def coerce_insert_arrow(ns: LanceNamespace, so: StorageOptions, table_id: list[str], data: bytes) -> bytes:
    """Align Arrow-IPC insert rows to the table's schema before the native append.

    A client that INFERS types loosely — most importantly the browser's apache-arrow, which infers
    ``float64`` for every JS number — otherwise hits the native ``insert_into_table`` with a ``float64``
    batch against an ``int64`` column, which raises a bare **500** ("Internal Server Error"). That is both
    a broken insert AND the wrong status for a client-side mismatch (browser-driven find 2026-07-21). Here we
    select the table's columns BY NAME (extra columns dropped; a missing one is a 400) and cast each to its
    real column type — so ``4.0 → 4`` just works — turning a genuinely incompatible payload (e.g. ``4.5`` →
    ``int``, or a non-castable type) into a clean ``400``, never a 500. Blocking IO (opens the dataset for the
    live schema); the caller runs it in a threadpool.

    An already-aligned payload passes through UNTOUCHED: re-serializing it cost two full
    materialisations (the IPC re-encode + ``to_pybytes``) on every insert from a schema-exact client —
    which is every non-browser client — for zero behavioural difference (#141).
    """
    incoming = pa.ipc.open_stream(data).read_all()
    target = open_dataset(ns, so, table_id).schema
    if [(f.name, f.type, f.nullable) for f in incoming.schema] == [(f.name, f.type, f.nullable) for f in target]:
        return data
    present = set(incoming.column_names)
    missing = [f.name for f in target if f.name not in present]
    if missing:
        raise InvalidInputError(f"insert rows are missing column(s): {', '.join(missing)}")
    try:
        selected = pa.table({f.name: incoming.column(f.name) for f in target})
        aligned = selected.cast(pa.schema([pa.field(f.name, f.type, f.nullable) for f in target]))
    except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError) as exc:
        raise InvalidInputError(f"insert rows don't match the table schema: {exc}") from exc
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, aligned.schema) as writer:
        writer.write_table(aligned)
    return sink.getvalue().to_pybytes()


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
    return UpdateFieldMetadataResponse(version=_version(ns, so, table_id, branch), fields=fields)


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
#: Anything not listed still gets its operation NAME, so a Merge/Compact/CreateIndex commit appears in the
#: log as itself rather than vanishing — an unknown operation is a gap in this table, never a gap in the
#: history.
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
    dataset = open_dataset(ns, so, table_id)
    versions = sorted(dataset.versions(), key=lambda v: int(v["version"]), reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for entry in versions:
        version = int(entry["version"])
        row: dict[str, Any] = {
            "version": version,
            "timestamp": entry.get("timestamp").isoformat() if entry.get("timestamp") else None,
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
        if op is None:
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


def list_tags(ns: LanceNamespace, so: StorageOptions, req: ListTableTagsRequest) -> ListTableTagsResponse:
    """List the table's tags as ``{name: TagContents{version, manifest_size, branch}}``."""
    table_id = _table_id(req)
    tags: dict[str, dict[str, Any]] = {}
    for name, tag in open_dataset(ns, so, table_id).tags.list().items():
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
    open_dataset(ns, so, _table_id(req)).tags.create(req.tag, _tag_reference(req.branch, req.version))
    return CreateTableTagResponse()


def get_tag_version(ns: LanceNamespace, so: StorageOptions, req: GetTableTagVersionRequest) -> GetTableTagVersionResponse:
    """Return the table version a tag points to (404 on an unknown tag)."""
    tags = open_dataset(ns, so, _table_id(req)).tags
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
    open_dataset(ns, so, _table_id(req)).tags.update(req.tag, _tag_reference(req.branch, req.version))
    return UpdateTableTagResponse()


def delete_tag(ns: LanceNamespace, so: StorageOptions, req: DeleteTableTagRequest) -> DeleteTableTagResponse:
    """Delete a tag from the table."""
    open_dataset(ns, so, _table_id(req)).tags.delete(req.tag)
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
    for name, branch in open_dataset(ns, so, _table_id(req)).branches.list().items():
        entry = branch if isinstance(branch, dict) else {}
        branches[name] = {
            "parent_branch": entry.get("parent_branch"),
            "parent_version": entry.get("parent_version"),
            "create_at": entry.get("create_at"),
            "manifest_size": entry.get("manifest_size") or 0,
            "metadata": entry.get("metadata") or {},
        }
    return ListTableBranchesResponse.model_validate({"branches": branches})


def create_branch(ns: LanceNamespace, so: StorageOptions, req: CreateTableBranchRequest) -> CreateTableBranchResponse:
    """Create a branch from main (or a source branch/version) — maps to pylance ``create_branch``."""
    open_dataset(ns, so, _table_id(req)).create_branch(req.name, _branch_reference(req))
    return CreateTableBranchResponse()


def delete_branch(ns: LanceNamespace, so: StorageOptions, req: DeleteTableBranchRequest) -> DeleteTableBranchResponse:
    """Delete a branch from the table."""
    open_dataset(ns, so, _table_id(req)).branches.delete(req.name)
    return DeleteTableBranchResponse()


def ensure_merge_key_index(ns: LanceNamespace, segments: list[str], on: str | None, *, branch: str | None = None) -> None:
    """Best-effort BTREE index on a merge key, built AFTER the first ``/merge_insert`` commits (§4).

    pylance's ``use_index=True`` default only helps *"if an index is available"*, and no automatic
    data-flow ever builds one — so without this, every upsert full-scans the ``on`` column and merge
    latency decays as the table grows (the namespace spec's own ``__manifest`` design mandates exactly
    this pairing: merge-insert PK dedup WITH a BTREE on the key).

    LIST FIRST is required, not an optimization: pylance's ``create_scalar_index`` defaults
    ``replace=True``, so an unconditional build would full-scan and REBUILD the column on every upsert
    — turning the fix into a regression. The build goes through the native op path
    (``create_table_scalar_index``) because it is the only path that carries ``branch``.
    NOTE: whether the dir backend honors ``branch`` on an index build is unverified at pylance 8.0.0
    (branch surfaces are dataplane-backed) — flagged for the live pass; the param is forwarded either way.

    Best-effort by contract: an index-build failure or a CreateIndex commit conflict must never fail
    the write that already committed — any failure logs and returns.
    """
    if not on:
        return
    try:
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
